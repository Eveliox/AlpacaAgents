from pathlib import Path
import ast
import json
import sys
from pypdf import PdfReader

root=Path(__file__).resolve().parent
sys.path.insert(0,str(root/'vendor'))
import pypdfium2 as pdfium

results=[]
for f in sorted((root.parent/'user-guides').glob('*.pdf')):
    r=PdfReader(f)
    page_ids={p.indirect_reference.idnum:i for i,p in enumerate(r.pages)}
    dests=[]; external=0
    for p in r.pages:
        for ref in p.get('/Annots',[]):
            a=ref.get_object()
            if '/Dest' in a:
                d=a['/Dest']; assert d[0].idnum in page_ids
                dests.append(page_ids[d[0].idnum])
            elif a.get('/A',{}).get('/URI'): external+=1
    expected=66 if f.name.startswith('AlpacaAgent') else 22
    assert len(dests)==expected,(f.name,len(dests))
    pdf=pdfium.PdfDocument(str(f)); bounds=[]
    for n in range(len(pdf)):
        page=pdf[n]; tp=page.get_textpage()
        for k in range(tp.count_chars()):
            ch=tp.get_text_range(k,1)
            if not ch.strip(): continue
            l,b,rr,t=tp.get_charbox(k)
            if l<45 or rr>568 or b<18 or t>773:
                bounds.append([n+1,ch,[l,b,rr,t]])
        tp.close(); page.close()
    pdf.close()
    assert not bounds,(f.name,bounds[:8])
    results.append({'file':f.name,'pages':len(r.pages),'internal_links':len(dests),'external_links':external,'bounds':'pass'})

# All embedded Python snippets in the source are syntactically valid.
for source in root.glob('0[123]-*.md'):
    for line in source.read_text(encoding='utf-8').splitlines():
        if line.startswith('python -c "') and line.endswith('"'):
            ast.parse(line[11:-1])

(root/'qa'/'verification.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
print(json.dumps(results,indent=2))
