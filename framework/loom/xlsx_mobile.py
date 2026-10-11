"""Reused mobile formatter from the reviewed owner XLSX patch r1."""
from datetime import datetime
import io
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile

def report_xlsx(title, table, *, labels=None, date_label='消息时间', note=None):
    """Compact vertical reading sheet; no private identifiers, formulas or external links."""
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rel = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    ET.register_namespace('', ns)
    tag = lambda name: '{'+ns+'}'+name
    sheet = ET.Element(tag('worksheet'))
    views = ET.SubElement(sheet,tag('sheetViews'))
    view = ET.SubElement(views,tag('sheetView'),{'workbookViewId':'0','showGridLines':'0'})
    pane=ET.SubElement(view,tag('pane'),{'ySplit':'4','topLeftCell':'A5','activePane':'bottomLeft','state':'frozen'})
    columns = ET.SubElement(sheet,tag('cols'))
    for col,width in ((1,14),(2,46)):
        ET.SubElement(columns,tag('col'),{'min':str(col),'max':str(col),'width':str(width),'customWidth':'1'})
    data = ET.SubElement(sheet,tag('sheetData')); merges=[];row_index=0

    def pieces(value, width=42, max_lines=9):
        # Explicit continuation rows avoid Excel's 409-point row-height limit.
        lines=[]
        for raw in str(value).split('\n'):
            line='';units=0
            for char in raw:
                amount=0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in ('W','F') else 1)
                if line and units+amount>width:lines.append(line);line='';units=0
                line+=char;units+=amount
            lines.append(line)
        return ['\n'.join(lines[i:i+max_lines]) for i in range(0,len(lines),max_lines)]

    def add(values, style=0, height=24, merge=False):
        nonlocal row_index
        row_index+=1
        row=ET.SubElement(data,tag('row'),{'r':str(row_index),'ht':str(height),'customHeight':'1'})
        for col,value in enumerate(values,1):
            cell=ET.SubElement(row,tag('c'),{'r':chr(64+col)+str(row_index),'s':str(style if merge or col==2 else 2)})
            if isinstance(value,tuple):
                cell.set('s','5');ET.SubElement(cell,tag('v')).text=str(value[0])
            else:
                cell.set('t','inlineStr');inside=ET.SubElement(cell,tag('is'))
                text=re.sub(r'_x[0-9A-Fa-f]{4}_',lambda m:'_x005F_'+m.group(0)[1:],str(value))
                text=re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]',lambda m:'_x'+format(ord(m.group(0)),'04X')+'_',text)
                ET.SubElement(inside,tag('t'),{'{http://www.w3.org/XML/1998/namespace}space':'preserve'}).text=text
        if merge:merges.append('A'+str(row_index)+':B'+str(row_index))

    for part in pieces(title,38,4):add([part],1,24*len(part.split('\n'))+12,True)
    add(['范围：'+table[1][7]],3,30,True)
    add(['资料更新：'+table[1][6]],3,30,True)
    add([note or '陈述依据原话，分析与建议供参考。时间为上海时间。'],3,42,True)
    pane.set('ySplit',str(row_index));pane.set('topLeftCell','A'+str(row_index+1))
    labels=labels or ('联系对象','结论类型','结论','原话','消息时间','发言人')
    for number,record in enumerate(table[1:],1):
        add(['记录 '+str(number)],4,28,True)
        for label,value in zip(labels,record[:6]):
            if label==date_label:
                moment=datetime.strptime(value,'%Y-%m-%d %H:%M (UTC+8)')
                add([label,((moment-datetime(1899,12,30)).total_seconds()/86400,)],height=30)
            else:
                for index,part in enumerate(pieces(value)):
                    add([label if index==0 else label+'（续）',part],height=max(30,18*len(part.split('\n'))+12))
        add(['',''],height=12)
    merged=ET.SubElement(sheet,tag('mergeCells'),{'count':str(len(merges))})
    for reference in merges:ET.SubElement(merged,tag('mergeCell'),{'ref':reference})
    ET.SubElement(sheet,tag('pageMargins'),{'left':'0.2','right':'0.2','top':'0.3','bottom':'0.3','header':'0','footer':'0'})
    styles='''<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm"/></numFmts><fonts count="3"><font><sz val="11"/><name val="Microsoft YaHei"/><color rgb="FF1D2433"/></font><font><b/><sz val="16"/><name val="Microsoft YaHei"/><color rgb="FFFFFFFF"/></font><font><b/><sz val="11"/><name val="Microsoft YaHei"/><color rgb="FF1D2433"/></font></fonts><fills count="4"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF1D5FAE"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFE8F0FC"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="6"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf><xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf><xf numFmtId="0" fontId="0" fillId="3" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf><xf numFmtId="0" fontId="2" fillId="3" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf><xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment vertical="top"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'''
    workbook=ET.Element(tag('workbook'))
    book_views=ET.SubElement(workbook,tag('bookViews'))
    ET.SubElement(book_views,tag('workbookView'),{'activeTab':'0'})
    sheets=ET.SubElement(workbook,tag('sheets'))
    ET.SubElement(sheets,tag('sheet'),{'name':'手机阅读','sheetId':'1','{'+rel+'}id':'rId1'})
    content_types='''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>'''
    relationships=lambda entries:'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join('<Relationship Id="'+i+'" Type="'+rel+'/'+t+'" Target="'+target+'"/>' for i,t,target in entries)+'</Relationships>'
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,value in {'[Content_Types].xml':content_types,'_rels/.rels':relationships([('rId1','officeDocument','xl/workbook.xml')]),
            'xl/workbook.xml':ET.tostring(workbook,encoding='utf-8',xml_declaration=True),
            'xl/_rels/workbook.xml.rels':relationships([('rId1','worksheet','worksheets/sheet1.xml'),('rId2','styles','styles.xml')]),
            'xl/styles.xml':styles,'xl/worksheets/sheet1.xml':ET.tostring(sheet,encoding='utf-8',xml_declaration=True)}.items():
            entry=zipfile.ZipInfo(name, date_time=(1980,1,1,0,0,0))
            entry.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(entry,value)
    return output.getvalue()


