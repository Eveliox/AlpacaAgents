"""Build the requested PDF guide set from the adjacent Markdown sources."""
from pathlib import Path
import re
import sys
import textwrap
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.enums import TA_LEFT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, PageBreak,
    Preformatted, Table, TableStyle, KeepTogether,
)
from reportlab.platypus.tableofcontents import TableOfContents
from pypdf import PdfReader, PdfWriter

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / 'user-guides'
QA = HERE / 'qa'
OUT.mkdir(exist_ok=True)
QA.mkdir(exist_ok=True)

for name, file in [('Guide','segoeui.ttf'),('GuideBold','segoeuib.ttf'),
                   ('GuideItalic','segoeuii.ttf'),('GuideMono','consola.ttf')]:
    pdfmetrics.registerFont(TTFont(name, str(Path('C:/Windows/Fonts')/file)))
pdfmetrics.registerFontFamily('Guide', normal='Guide', bold='GuideBold', italic='GuideItalic', boldItalic='GuideBold')
INK = colors.HexColor('#222B3C')
PLUM = colors.HexColor('#633B72')
TEAL = colors.HexColor('#137F83')
MUTED = colors.HexColor('#687285')
PALE = colors.HexColor('#F3F0F5')
RULE = colors.HexColor('#DDDCE3')
styles = {}
styles['body'] = ParagraphStyle('body', fontName='Guide', fontSize=9.8, leading=13.5, textColor=INK, spaceAfter=6)
styles['h1'] = ParagraphStyle('h1', fontName='GuideBold', fontSize=22, leading=27, textColor=PLUM, spaceAfter=17, keepWithNext=True)
styles['h2'] = ParagraphStyle('h2', fontName='GuideBold', fontSize=12, leading=16, textColor=INK, spaceBefore=9, spaceAfter=5, keepWithNext=True)
styles['bullet'] = ParagraphStyle('bullet', parent=styles['body'], leftIndent=14, firstLineIndent=-10, spaceAfter=5)
styles['quote'] = ParagraphStyle('quote', parent=styles['body'], leftIndent=11, rightIndent=10, borderColor=TEAL, borderWidth=1, borderPadding=6, backColor=colors.HexColor('#EDF6F5'), spaceBefore=9, spaceAfter=13)
styles['code'] = ParagraphStyle('code', fontName='GuideMono', fontSize=8, leading=10, textColor=INK, backColor=PALE, borderPadding=9, spaceBefore=6, spaceAfter=10)
styles['cell'] = ParagraphStyle('cell', parent=styles['body'], fontSize=8.2, leading=11.2, spaceAfter=0)
styles['cellhead'] = ParagraphStyle('cellhead', parent=styles['cell'], fontName='GuideBold', textColor=colors.white)
styles['small'] = ParagraphStyle('small', parent=styles['body'], fontSize=9, leading=13, textColor=MUTED)
styles['title'] = ParagraphStyle('title', fontName='GuideBold', fontSize=42, leading=48, textColor=PLUM, spaceAfter=20)
styles['subtitle'] = ParagraphStyle('subtitle', fontName='GuideBold', fontSize=21, leading=28, textColor=INK, spaceAfter=24)
styles['deck'] = ParagraphStyle('deck', parent=styles['body'], fontSize=12.5, leading=19, spaceAfter=24)

def inline(s):
    s = escape(s)
    s = re.sub(r'\[([^\]]+)\]\((https://[^)]+)\)', r'<link href="\2" color="#137F83"><u>\1</u></link>', s)
    s = re.sub(r'`([^`]+)`', r'<font name="GuideMono">\1</font>', s)
    return s

class GuideDoc(BaseDocTemplate):
    def __init__(self, filename, meta):
        super().__init__(str(filename), pagesize=(612,792), leftMargin=53, rightMargin=53,
                         topMargin=57, bottomMargin=53, title=meta['title']+' - '+meta['subtitle'],
                         author='AlpacaAgent operator documentation')
        self.meta = meta
        self.current_chapter = ''
        self.addPageTemplates(PageTemplate(id='guide', frames=[Frame(53,53,506,682,id='body',leftPadding=0,rightPadding=0,topPadding=0,bottomPadding=0)], onPage=self.decorate))
    def beforeDocument(self):
        self.current_chapter = ''
    def decorate(self,c,doc):
        c.saveState()
        if doc.page == 1:
            c.setFillColor(PLUM); c.rect(0,758,612,34,fill=1,stroke=0)
            c.setFillColor(TEAL); c.rect(53,90,60,5,fill=1,stroke=0)
        else:
            c.setFont('GuideBold',8); c.setFillColor(PLUM)
            c.drawString(53,761,'ALPACAAGENT / '+self.meta['volume'])
            c.setStrokeColor(RULE); c.line(53,751,559,751)
        c.setStrokeColor(RULE); c.line(53,40,559,40)
        c.setFont('Guide',7.6); c.setFillColor(MUTED)
        c.drawString(53,26,'Paper-only operator guide | 18 September 2026 | revision 235b087')
        c.drawRightString(559,26,str(doc.page))
        c.restoreState()
    def afterFlowable(self,flowable):
        if isinstance(flowable,Paragraph) and flowable.style.name == 'h1' and hasattr(flowable,'bookmark'):
            key = flowable.bookmark
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(flowable.getPlainText(),key,0,False)
            self.notify('TOCEntry',(0,flowable.getPlainText(),self.page,key))

def parse_body(lines):
    flow=[]; i=0; chapter=0
    while i<len(lines):
        line=lines[i].strip()
        if not line or line.startswith('@'):
            i+=1; continue
        if line.startswith('# '):
            flow.append(PageBreak()); chapter+=1
            p=Paragraph(inline(line[2:]),styles['h1']); p.bookmark='chapter-'+str(chapter)
            flow.append(p); i+=1; continue
        if line.startswith('## '):
            flow.append(Paragraph(inline(line[3:]),styles['h2'])); i+=1; continue
        if line.startswith('```'):
            code=[]; i+=1
            while i<len(lines) and not lines[i].startswith('```'):
                value = lines[i]
                if len(value)>106 and value.startswith('python -c "') and value.endswith('"'):
                    code.extend(["@'"]+value[11:-1].split('; ')+["'@ | python"])
                else:
                    assert len(value)<=112, f'Long command needs manual wrapping: {value}'
                    code.append(value)
                i+=1
            flow.append(Preformatted('\n'.join(code),styles['code'])); i+=1; continue
        if line.startswith('|'):
            rows=[]
            while i<len(lines) and lines[i].strip().startswith('|'):
                cols=[x.strip() for x in lines[i].strip().strip('|').split('|')]
                if not all(re.fullmatch(r'[-: ]+',x) for x in cols): rows.append(cols)
                i+=1
            n=len(rows[0]); widths=[506/n]*n
            if n==2: widths=[166,340]
            if n==3: widths=[127,183,196]
            data=[[Paragraph(inline(x),styles['cellhead'] if r==0 else styles['cell']) for x in row] for r,row in enumerate(rows)]
            t=Table(data,colWidths=widths,repeatRows=1,hAlign='LEFT')
            t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),PLUM),('VALIGN',(0,0),(-1,-1),'TOP'),
                                   ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#F7F8FA')]),
                                   ('LINEBELOW',(0,0),(-1,-1),0.4,RULE),('LEFTPADDING',(0,0),(-1,-1),7),
                                   ('RIGHTPADDING',(0,0),(-1,-1),7),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5)]))
            flow.extend([t,Spacer(1,10)]); continue
        if line.startswith('> '):
            flow.append(Paragraph(inline(line[2:]),styles['quote'])); i+=1; continue
        if line.startswith('- '):
            flow.append(Paragraph('• '+inline(line[2:]),styles['bullet'])); i+=1; continue
        if re.match(r'^\d+\. ',line):
            flow.append(Paragraph(inline(line),styles['bullet'])); i+=1; continue
        chunk=[line]; i+=1
        while i<len(lines) and lines[i].strip() and not re.match(r'^(#|>|\||```|- |\d+\. )',lines[i]):
            chunk.append(lines[i].strip()); i+=1
        flow.append(Paragraph(inline(' '.join(chunk)),styles['body']))
    return flow

def build(source):
    raw=source.read_text(encoding='utf-8')
    meta=dict(re.findall(r'^@(\w+) (.+)$',raw,re.M))
    output=OUT/(source.stem+'.pdf')
    doc=GuideDoc(output,meta)
    story=[Spacer(1,62),Paragraph(meta['volume'],styles['small']),Spacer(1,27),
           Paragraph(meta['title'],styles['title']),Paragraph(meta['subtitle'],styles['subtitle']),
           Paragraph(meta['description'],styles['deck']),Spacer(1,32),
           Paragraph('DETAILED WINDOWS EDITION',styles['h2']),
           Paragraph('Based on the local implementation, with corrected commands, practical examples, and explicit operational boundaries.',styles['body']),
           Spacer(1,23),Paragraph('Prepared 18 September 2026<br/>Source revision 235b087<br/>Companion volumes: handbook, use cases, and desk reference',styles['small']),PageBreak(),
           Paragraph('Contents',styles['h1']),Paragraph('Choose a chapter below. PDF bookmarks and contents links navigate directly to each chapter.',styles['small'])]
    toc=TableOfContents()
    toc.levelStyles=[ParagraphStyle('toc',fontName='Guide',fontSize=10.2,leading=16,textColor=INK,spaceBefore=10,leftIndent=0,firstLineIndent=0)]
    story.append(toc)
    story.extend(parse_body(raw.splitlines()))
    doc.multiBuild(story)
    reader=PdfReader(output)
    texts=[p.extract_text() or '' for p in reader.pages]
    assert all(len(t)>100 for t in texts), 'Unexpected nearly empty page'
    assert 'Contents' in texts[1]
    print(f'{output.name}: {len(texts)} pages; {sum(len(t.split()) for t in texts)} words')
    (QA/(source.stem+'.txt')).write_text('\n\n'.join(texts),encoding='utf-8')
    return output

if __name__=='__main__':
    outputs=[build(p) for p in sorted(HERE.glob('0[123]-*.md'))]
    combined=PdfWriter()
    for path in outputs:
        reader=PdfReader(path)
        offset=len(combined.pages)
        combined.append(reader,outline_item=path.stem[3:].replace('-',' ').title(),excluded_fields=['/Annots'])
        # Explicitly remap direct ReportLab destinations; preserve every link.
        from pypdf.generic import DictionaryObject, NameObject, ArrayObject
        source_pages={p.indirect_reference.idnum:n for n,p in enumerate(reader.pages)}
        for n,p in enumerate(reader.pages):
            for ref in p.get('/Annots',[]):
                old=ref.get_object()
                annot=DictionaryObject()
                for key,val in old.items():
                    if key not in ('/Dest','/P'): annot[NameObject(key)]=val.clone(combined)
                if '/Dest' in old:
                    dest=old['/Dest']; target=source_pages[dest[0].idnum]+offset
                    annot[NameObject('/Dest')]=ArrayObject([combined.pages[target].indirect_reference]+[v.clone(combined) for v in dest[1:]])
                page=combined.pages[offset+n]
                if '/Annots' not in page: page[NameObject('/Annots')]=ArrayObject()
                page['/Annots'].append(combined._add_object(annot))
    with (OUT/'AlpacaAgent-Complete-Guide.pdf').open('wb') as f: combined.write(f)
    sys.path.insert(0,str(HERE/'vendor'))
    import pypdfium2 as pdfium
    from PIL import Image, ImageOps, ImageDraw
    for path in outputs:
        pdf=pdfium.PdfDocument(str(path)); thumbs=[]
        for idx in range(len(pdf)):
            page=pdf[idx]; img=page.render(scale=1.15).to_pil().convert('RGB')
            img.save(QA/f'{path.stem}-p{idx+1:02}.png')
            img.thumbnail((306,396))
            tile=Image.new('RGB',(326,424),'#E2E4E9'); tile.paste(img,((326-img.width)//2,6))
            ImageDraw.Draw(tile).text((12,405),f'{path.stem[:2]} / page {idx+1}',fill='black')
            thumbs.append(tile)
            page.close()
        for batch in range(0,len(thumbs),9):
            sheet=Image.new('RGB',(978,424*((len(thumbs[batch:batch+9])+2)//3)),'white')
            for n,t in enumerate(thumbs[batch:batch+9]): sheet.paste(t,((n%3)*326,(n//3)*424))
            sheet.save(QA/f'{path.stem}-contact-{batch//9+1}.png')
        pdf.close()
    print('Built combined edition and rendered all individual pages for QA.')
