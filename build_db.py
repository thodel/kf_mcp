import argparse, glob, re, sqlite3, sys, time, xml.etree.ElementTree as ET

NS = 'http://www.tei-c.org/ns/1.0'
XMLID = '{http://www.w3.org/XML/1998/namespace}id'

DDL = """
PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS entries(id TEXT PRIMARY KEY,title TEXT,short_id TEXT,year INTEGER,source TEXT,pages TEXT,text_raw TEXT);
CREATE TABLE IF NOT EXISTS spans(id INTEGER PRIMARY KEY AUTOINCREMENT,entry_id TEXT,span_id TEXT,class TEXT,ref TEXT,text TEXT,norm TEXT);
CREATE TABLE IF NOT EXISTS persons(id TEXT PRIMARY KEY,forename TEXT,surname TEXT,full_name TEXT,main_name TEXT,occupation TEXT,birth TEXT,death TEXT,org_ref TEXT,hls_id TEXT,note TEXT);
CREATE TABLE IF NOT EXISTS places(id TEXT PRIMARY KEY,name_de TEXT,name_fr TEXT,country TEXT,region TEXT,geo TEXT,hls_id TEXT,gnd_id TEXT,place_type TEXT);
CREATE TABLE IF NOT EXISTS orgs(id TEXT PRIMARY KEY,name TEXT,desc_de TEXT,desc_fr TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_entries USING fts5(id UNINDEXED,title UNINDEXED,text_raw,content=entries,content_rowid=rowid);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_spans USING fts5(entry_id UNINDEXED,ref UNINDEXED,class UNINDEXED,text,content=spans,content_rowid=rowid);
"""
TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN INSERT INTO fts_entries(rowid,id,title,text_raw)VALUES(new.rowid,new.id,new.title,new.text_raw);END;
CREATE TRIGGER IF NOT EXISTS spans_ai AFTER INSERT ON spans BEGIN INSERT INTO fts_spans(rowid,entry_id,ref,class,text)VALUES(new.rowid,new.entry_id,new.ref,new.class,new.text);END;
"""
# A build always reflects the full corpus, so it starts from empty tables. Without
# this, spans (plain INSERT, autoincrement PK) are duplicated on every rerun while
# entries (INSERT OR IGNORE) are not. Only the tables this script owns are touched.
RESET = """
DROP TRIGGER IF EXISTS entries_ai; DROP TRIGGER IF EXISTS spans_ai;
DROP TABLE IF EXISTS fts_entries; DROP TABLE IF EXISTS fts_spans;
DELETE FROM entries; DELETE FROM spans; DELETE FROM persons; DELETE FROM places; DELETE FROM orgs;
DELETE FROM sqlite_sequence WHERE name='spans';
"""

def tag(local): return f'{{{NS}}}{local}'
def init_db(path):
    con=sqlite3.connect(path); con.executescript(DDL); con.executescript(TRIGGERS)
    n=con.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    if n: print(f"Existing database: clearing {n:,} entries and rebuilding from source")
    con.executescript(RESET)
    con.executescript(DDL); con.executescript(TRIGGERS)   # recreate the dropped FTS tables + triggers
    con.commit(); return con
# Elements whose content is part of the surrounding word — abbreviation machinery and
# editorial marks. Everything else counts as a word boundary, which is safe: a space is
# only ever inserted where two word characters would otherwise collide, so an element
# that already has whitespace around it in the source is unaffected either way.
WORD_INTERNAL = {'ex','am','abbr','expan','choice','orig','reg','sic','corr','supplied',
                 'unclear','damage','del','add','subst','surplus','hi','g','c','gap'}
# Milestones: a line/page/column break separates words unless it carries break="no".
BREAK_TAGS = {'lb','pb','cb'}

# A word character, or the punctuation that closes a token — after either of these a
# following word needs a space. Opening marks ( « „ and the hyphen are excluded, so
# '(Heinrich' and 'Kloster-felden' stay flush.
_ENDS_TOKEN  = re.compile(r'[\w,.;:!?)\]}»”"\'…]$')
_STARTS_WORD = re.compile(r'^\w')

def word_internal(el):
    if not isinstance(el.tag, str): return False   # comments / processing instructions
    local=el.tag.rsplit('}',1)[-1]
    if local in BREAK_TAGS: return el.get('break')=='no'
    return local in WORD_INTERNAL

def join_text(left, right):
    """Concatenate two fragments, inserting a space only where two word characters
    would otherwise run together ('1350' + 'Heinrich'). Punctuation is left flush."""
    if not left: return right
    if not right: return left
    if _ENDS_TOKEN.search(left) and _STARTS_WORD.match(right): return left+' '+right
    return left+right

def extract_text(elem):
    """Flatten an element to text. Adjacent markup with no whitespace between the tags
    would otherwise glue words together and make them unsearchable."""
    out=elem.text or ''
    for c in elem:
        inner=extract_text(c); tail=c.tail or ''
        if word_internal(c): out=out+inner+tail          # splice into the word
        else: out=join_text(join_text(out,inner),tail)
    return out

def norm_text(elem):
    """extract_text with whitespace collapsed — what actually goes into the database."""
    return ' '.join(extract_text(elem).split())

# The corpus runs 1300–1658; the upper bound is deliberately loose, it only has to
# exclude edition/publication years that would otherwise pass as document dates.
YEAR_MIN, YEAR_MAX = 1000, 1800

def first_year(dates):
    """First plausible document year from a sequence of <date> elements, else None."""
    for d in dates:
        when=d.get('when','')
        if not when or when=='0001-01-01': continue
        try: y=int(when[:4])
        except ValueError: continue
        if YEAR_MIN < y <= YEAR_MAX: return y
    return None

def parse_entry(tree, entry_id):
    root=tree.getroot()
    hdr=root.find(f'.//{tag("teiHeader")}'); fd=hdr.find(f'.//{tag("fileDesc")}') if hdr is not None else None
    title=short_id=source=None; pages=[]
    if fd is not None:
        t=fd.find(f'{tag("titleStmt")}/{tag("title")}'); title=t.text.strip() if t is not None and t.text else None
        for i in fd.findall(f'.//{tag("idno")}'):
            if i.get('type')=='short' and i.text: short_id=i.text.strip(); break
        for i in fd.findall(f'.//{tag("idno")}'):
            n=i.get('n',''); 
            if n: pages.append(n.strip())
        s=fd.find(f'{tag("seriesStmt")}/{tag("title")}'); source=s.text.strip() if s is not None and s.text else None
    body=root.find(f'.//{tag("body")}'); text_raw=norm_text(body) if body is not None else ''
    # Only the body dates the document. Searching from root would hit teiHeader first
    # (publicationStmt, revisionDesc) and stamp entries with the edition's year.
    year=first_year(body.findall(f'.//{tag("date")}')) if body is not None else None
    if year is None and fd is not None:
        sd=fd.find(f'{tag("sourceDesc")}')   # describes the source charter, not the edition
        if sd is not None: year=first_year(sd.findall(f'.//{tag("date")}'))
    entry=(entry_id,title,short_id,year,source,' | '.join(pages) if pages else None,text_raw)
    spans=[]
    for cls,local in [('persName','persName'),('placeName','placeName'),('orgName','orgName'),('date','date'),('measure','measure')]:
        for el in root.findall(f'.//{tag(local)}'):
            ref=el.get('ref',''); text=norm_text(el)
            if not text: continue
            norm=el.get('when') or el.get('quantity') or ''
            spans.append((entry_id,el.get(XMLID,''),cls,ref,text,norm))
    return entry,spans

def open_list(path, list_tag, label):
    """Root list element of an authority file, or None (file-level failure only)."""
    try:
        return ET.parse(path).getroot().find(f'.//{tag(list_tag)}')
    except Exception as e:
        print(f"  {label} file error: {e}",file=sys.stderr); return None

def report_skipped(n_bad, label):
    if n_bad: print(f"  WARNING: {n_bad} {label} record(s) skipped due to errors",file=sys.stderr)

def parse_people(path):
    records=[]; n_bad=0
    lp=open_list(path,"listPerson","people")
    if lp is None: return records
    for person in lp.findall(f'{tag("person")}'):
        try:
            pid=person.get(XMLID,'')
            forename=surname=full_name=main_name=occ=birth=death=org_ref=hls_id=note=None
            for pn in person.findall(f'{tag("persName")}'):
                text=norm_text(pn)
                if not text: continue
                if pn.get('type')=='full':
                    fn=pn.find(f'{tag("forename")}'); sn=pn.find(f'{tag("surname")}')
                    forename=fn.text.strip() if fn is not None and fn.text else ''
                    surname=sn.text.strip() if sn is not None and sn.text else ''
                    full_name=text
                elif pn.get('type')=='main' and not main_name: main_name=text
            o=person.find(f'{tag("occupation")}'); occ=o.text.strip() if o is not None and o.text else None
            b=person.find(f'{tag("birth")}'); birth=b.text.strip() if b is not None and b.text else None
            d=person.find(f'{tag("death")}'); death=d.text.strip() if d is not None and d.text else None
            aff=person.find(f'{tag("affiliation")}'); org_ref=aff.get('ref','') if aff is not None else None
            for bibl in person.findall(f'{tag("bibl")}'):
                for idno in bibl.findall(f'{tag("idno")}'):
                    if idno.get('type')=='HLS' and idno.text: hls_id=idno.text.strip(); break
            n=person.find(f'{tag("note")}'); note=n.text.strip() if n is not None and n.text else None
            records.append([pid,forename,surname,full_name,main_name,occ,birth,death,org_ref,hls_id,note])
        except Exception as e:
            n_bad+=1; print(f"  person {person.get(XMLID,'?')}: {e}",file=sys.stderr)
    report_skipped(n_bad,"person")
    return records

def parse_places(path):
    records=[]; n_bad=0
    lp=open_list(path,"listPlace","places")
    if lp is None: return records
    for place in lp.findall(f'{tag("place")}'):
        try:
            pid=place.get(XMLID,''); name_de=name_fr=country=region=geo=hls_id=gnd_id=place_type=None
            for pn in place.findall(f'{tag("placeName")}'):
                text=norm_text(pn)
                if not text: continue
                lang=pn.get('{http://www.w3.org/XML/1998/namespace}lang','')
                if lang in('deu','de'): name_de=text
                elif lang in('fra','fr'): name_fr=text
                elif not name_de: name_de=text
            loc=place.find(f'{tag("location")}')
            if loc is not None: country=loc.findtext(f'{tag("country")}'); region=loc.findtext(f'{tag("region")}'); geo=loc.findtext(f'{tag("geo")}')
            for bibl in place.findall(f'{tag("bibl")}'):
                for idno in bibl.findall(f'{tag("idno")}'):
                    t=idno.get('type',''); txt=idno.text or ''
                    if t=='HLS': hls_id=txt.strip()
                    elif t=='GND': gnd_id=txt.strip()
            n=place.find(f'{tag("note")}'); place_type=n.get('type','') if n is not None else None
            records.append([pid,name_de,name_fr,country,region,geo,hls_id,gnd_id,place_type])
        except Exception as e:
            n_bad+=1; print(f"  place {place.get(XMLID,'?')}: {e}",file=sys.stderr)
    report_skipped(n_bad,"place")
    return records

def parse_orgs(path):
    records=[]; n_bad=0
    lo=open_list(path,"listOrg","orgs")
    if lo is None: return records
    for org in lo.findall(f'{tag("org")}'):
        try:
            oid=org.get(XMLID,'')
            on=org.find(f'{tag("orgName")}'); name=norm_text(on) if on is not None else None
            desc_de=desc_fr=None
            for d in org.findall(f'{tag("desc")}'):
                text=d.text.strip() if d.text else ''; lang=d.get('{http://www.w3.org/XML/1998/namespace}lang','')
                if lang in('deu','de'): desc_de=text
                elif lang in('fra','fr'): desc_fr=text
            records.append([oid,name,desc_de,desc_fr])
        except Exception as e:
            n_bad+=1; print(f"  org {org.get(XMLID,'?')}: {e}",file=sys.stderr)
    report_skipped(n_bad,"org")
    return records

def build(docs_dir, regs_dir, db_path, batch=200):
    t0=time.time(); con=init_db(db_path); cur=con.cursor()
    print("Parsing authority files…")
    people=parse_people(f"{regs_dir}/people.xml"); print(f"  {len(people)} persons")
    places=parse_places(f"{regs_dir}/places.xml"); print(f"  {len(places)} places")
    orgs=parse_orgs(f"{regs_dir}/organizations.xml"); print(f"  {len(orgs)} orgs")
    cur.executemany("INSERT OR IGNORE INTO persons VALUES (?,?,?,?,?,?,?,?,?,?,?)",people)
    cur.executemany("INSERT OR IGNORE INTO places VALUES (?,?,?,?,?,?,?,?,?)",places)
    cur.executemany("INSERT OR IGNORE INTO orgs VALUES (?,?,?,?)",orgs)
    con.commit()
    xml_files=sorted(glob.glob(f"{docs_dir}/*.xml"))
    print(f"\nParsing {len(xml_files)} document files…")
    entry_rows=[]; span_rows=[]; n=0
    for fpath in xml_files:
        fname=fpath.rsplit('/',1)[-1].rsplit('.',1)[0]
        try:
            tree=ET.parse(fpath); entry,spans=parse_entry(tree,fname); entry_rows.append(entry)
            for s in spans: span_rows.append(s); n+=1
        except Exception as e: print(f"\n  Error {fname}: {e}",file=sys.stderr)
        if n%batch==0:
            cur.executemany("INSERT OR IGNORE INTO entries VALUES (?,?,?,?,?,?,?)",entry_rows)
            cur.executemany("INSERT INTO spans(entry_id,span_id,class,ref,text,norm) VALUES (?,?,?,?,?,?)",span_rows)
            con.commit(); entry_rows=[]; span_rows=[]; print(f"  {n:,} entries parsed…",end='\r',flush=True)
    cur.executemany("INSERT OR IGNORE INTO entries VALUES (?,?,?,?,?,?,?)",entry_rows)
    cur.executemany("INSERT INTO spans(entry_id,span_id,class,ref,text,norm) VALUES (?,?,?,?,?,?)",span_rows)
    con.commit()
    elapsed=time.time()-t0
    print(f"\n\nDone in {elapsed:.1f}s")
    for lbl in("entries","spans","persons","places","orgs"):
        n2=cur.execute(f"SELECT COUNT(*) FROM {lbl}").fetchone()[0]; print(f"  {lbl}: {n2:,}")
    con.close()

if __name__=='__main__':
    ap=argparse.ArgumentParser(); ap.add_argument('--docs',default='../data/docs'); ap.add_argument('--registers',default='../data/registers'); ap.add_argument('--db',default='kf.db'); ap.add_argument('--batch',type=int,default=200)
    args=ap.parse_args(); build(args.docs,args.registers,args.db,args.batch)
