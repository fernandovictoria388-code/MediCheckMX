from pathlib import Path
import requests, re, json, datetime, sqlite3, os, unicodedata, time
from bs4 import BeautifulSoup
from pypdf import PdfReader

BASE=Path(__file__).parent
DATA=BASE/'data'
RAW=DATA/'cofepris_raw'
RAW.mkdir(parents=True, exist_ok=True)
PAGE='https://www.gob.mx/cofepris/documentos/registros-sanitarios-medicamentos'
VIEWER='https://registros.cofepris.gob.mx/BRSDM/'
HEAD={'User-Agent':'MediCheckMX/0.29 official-source-sync'}


def norm(s):
    s=unicodedata.normalize('NFKD',str(s or '')).encode('ascii','ignore').decode('ascii')
    return re.sub(r'\s+',' ',s).upper().strip()


def get_years(env_name, default):
    return [x.strip() for x in os.getenv(env_name,default).split(',') if x.strip()]


def discover():
    r=requests.get(PAGE,headers=HEAD,timeout=60)
    r.raise_for_status()
    soup=BeautifulSoup(r.text,'html.parser')
    links=[]
    for a in soup.find_all('a',href=True):
        txt=' '.join(a.stripped_strings)
        href=a.get('href','').strip()
        if href.startswith('/'): href='https://www.gob.mx'+href
        links.append((txt,href))

    out=[]
    record_years=get_years('COFEPRIS_YEARS','2026')
    status_years=get_years('COFEPRIS_STATUS_YEARS','2025')
    cats=[('alopaticos','Registros_Alopaticos_otorgados_'),('herbolarios','Registros_Herbolarios_otorgados_'),('vitaminicos','Registros_Vitaminicos_otorgados_'),('homeopaticos','Registros_Homeopaticos_otorgados_')]

    def find(label):
        target=norm(label)
        for txt,href in links:
            if target in norm(txt) or target in norm(href): return href
        return None

    for year in record_years:
        for key,prefix in cats:
            label=prefix+year
            href=find(label)
            if href: out.append({'key':f'{key}_{year}','label':label,'url':href,'kind':'record','year':year})

    for year in status_years:
        for status,label in [('REVOKED',f'Registros Revocados Medicamentos {year}'),('CANCELLED',f'Registros Cancelados Medicamentos {year}')]:
            href=find(label)
            if href: out.append({'key':f'{status.lower()}_{year}','label':label,'url':href,'kind':'status','year':year,'status':status})
    return out


def pdf_text(path):
    chunks=[]
    reader=PdfReader(path)
    for page in reader.pages:
        try: t=page.extract_text(extraction_mode='layout') or ''
        except Exception: t=page.extract_text() or ''
        chunks.append(t)
    return '\n'.join(chunks)

REG_PATTERNS=[
    re.compile(r'\b\d{1,8}[A-Z]?\d{0,4}\s*(?:SSA|SS[A-Z]?|MEX)\b',re.I),
    re.compile(r'\b[A-Z0-9]{1,10}\s*\d{2,6}\s*SSA\b',re.I),
]

def reg_match(line):
    for p in REG_PATTERNS:
        m=p.search(line)
        if m: return re.sub(r'\s+',' ',m.group(0)).strip()
    return ''


def clean_cell(x):
    return re.sub(r'\s+',' ',str(x or '')).strip(' |\t')


def parse_record_lines(text,url,date):
    lines=[clean_cell(x) for x in text.splitlines() if clean_cell(x)]
    rows=[]
    headers=[]
    for i,line in enumerate(lines):
        n=norm(line)
        if 'NUMERO' in n and 'REGISTRO' in n: headers.append(i)

    # Generic parser: preserves the complete source row in source_fragment even when
    # a PDF's visual columns cannot be reliably separated.
    for i,line in enumerate(lines):
        reg=reg_match(line)
        if not reg: continue
        before=line[:max(0,line.upper().find(reg.upper()))].strip(' |')
        after=line[max(0,line.upper().find(reg.upper()))+len(reg):].strip(' |')
        name=before or after
        if not name and i+1<len(lines): name=lines[i+1]
        if norm(name) in {norm(reg),'REGISTRO','NUMERO REGISTRO'}: name=''
        # Avoid treating obvious header/metadata rows as medicine names.
        if norm(name) in {'NOMBRE','DENOMINACION DISTINTIVA','DENOMINACION GENERICA','TITULAR'}: name=''
        rows.append((reg,name,'','','','', 'ACTIVE',url,date,line))

    seen=set(); unique=[]
    for x in rows:
        key=(norm(x[0]),norm(x[1]))
        if key not in seen:
            seen.add(key); unique.append(x)
    return unique


def download(url,dest):
    last=None
    for attempt in range(3):
        try:
            r=requests.get(url,headers=HEAD,timeout=120,allow_redirects=True)
            r.raise_for_status()
            if len(r.content)<1000: raise RuntimeError('archivo demasiado pequeño')
            dest.write_bytes(r.content)
            return r
        except Exception as e:
            last=e; time.sleep(1.5*(attempt+1))
    raise last


def sync_official_sources():
    from app import db
    c=db(); now=datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
    found=discover(); downloaded=[]; total=0; stat_total=0; failures=[]
    if not found:
        raise RuntimeError('COFEPRIS no devolvió enlaces compatibles. La página oficial puede haber cambiado.')

    for src in found:
        try:
            dest=RAW/(src['key']+'.pdf')
            rr=download(src['url'],dest)
            ctype=(rr.headers.get('content-type') or '').lower()
            text=pdf_text(dest)
            rows=[]
            if src['kind']=='status':
                for line in text.splitlines():
                    line=clean_cell(line); reg=reg_match(line)
                    if reg:
                        c.execute('INSERT OR REPLACE INTO statuses VALUES (?,?,?,?,?)',(norm(reg),src['status'],line,src['url'],now[:10])); stat_total+=1
            else:
                rows=parse_record_lines(text,src['url'],now[:10])
                for row in rows:
                    c.execute('''INSERT OR REPLACE INTO records
                    (registry,name,active_ingredient,concentration,presentation,manufacturer,status,source_url,source_date,source_fragment)
                    VALUES (?,?,?,?,?,?,?,?,?,?)''',row)
                    total+=1
            c.execute('INSERT OR REPLACE INTO sync_sources VALUES (?,?,?,?,?,?,?,?)',(src['key'],src['label'],src['url'],src['kind'],src['year'],now,len(rr.content),len(rows)))
            downloaded.append({'key':src['key'],'label':src['label'],'url':src['url'],'bytes':len(rr.content),'records':len(rows),'kind':src['kind'],'content_type':ctype})
        except Exception as e:
            failures.append({'key':src['key'],'url':src['url'],'error':str(e)})

    c.commit()
    manifest={'last_sync':now,'official_page':PAGE,'viewer':VIEWER,'sources':downloaded,'failures':failures,'configured_record_years':get_years('COFEPRIS_YEARS','2026'),'configured_status_years':get_years('COFEPRIS_STATUS_YEARS','2025')}
    (DATA/'sync_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    if not downloaded:
        raise RuntimeError('No se pudo descargar ninguna fuente oficial. Revisa conectividad del servidor.')
    return {'ok':True,'last_sync':now,'record_count':c.execute('SELECT COUNT(*) FROM records').fetchone()[0],'status_count':c.execute('SELECT COUNT(*) FROM statuses').fetchone()[0],'downloaded':downloaded,'failures':failures,'official_page':PAGE,'viewer':VIEWER,'note':'Los datos provienen de enlaces descubiertos en la página oficial de COFEPRIS. Los campos que no pueden extraerse de forma segura del PDF quedan vacíos; no se inventan.'}

if __name__=='__main__': print(json.dumps(sync_official_sources(),ensure_ascii=False,indent=2))
