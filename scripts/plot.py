"""Rebuild measured charts, the editable report and its PDF. Requires reportlab and pypdfium2."""
from pathlib import Path
import csv
import io
import statistics as st
import subprocess
import textwrap
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                               TableStyle, PageBreak, Preformatted, Image)
from reportlab.graphics.shapes import Drawing, String, Rect, Line
from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.widgets.markers import makeMarker
from reportlab.graphics import renderPDF, renderSVG
import pypdfium2 as pdfium

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'report'
REPORT.mkdir(exist_ok=True)
pdfmetrics.registerFont(TTFont('Body', 'C:/Windows/Fonts/arial.ttf'))
pdfmetrics.registerFont(TTFont('Bold', 'C:/Windows/Fonts/arialbd.ttf'))
pdfmetrics.registerFont(TTFont('Code', 'C:/Windows/Fonts/consola.ttf'))
pdfmetrics.registerFontFamily('Body', normal='Body', bold='Bold', italic='Body', boldItalic='Bold')
rows = list(csv.DictReader((ROOT/'data/experiments.csv').open(encoding='utf-8-sig')))
assert len(rows) == 24
groups = {}
for row in rows:
    assert int(row['violations']) == int(row['errors']) == int(row['timeouts']) == 0
    assert row['operations'] == row['completed']
    key = (row['load'], row['mode'], int(row['workers']))
    groups.setdefault(key, []).append(row)
assert all(len(g) == 3 for g in groups.values())

def mean(key, field):
    return st.mean(float(r[field]) for r in groups[key])

def chart(field, title, ylabel, filename):
    drawing = Drawing(470, 265)
    drawing.add(String(235, 248, title, fontName='Bold', fontSize=12, textAnchor='middle'))
    plot = LinePlot()
    plot.x, plot.y, plot.width, plot.height = 60, 62, 375, 160
    series = [('standard','global'), ('standard','stage'), ('individual','global'), ('individual','stage')]
    palette = [colors.HexColor(c) for c in ['#2458a6','#0595a6','#b75b18','#8d4fa6']]
    plot.data = [[(n, mean((load,mode,n),field)) for n in (2,4)] for load,mode in series]
    plot.xValueAxis.valueMin, plot.xValueAxis.valueMax = 1.8, 4.2
    plot.xValueAxis.valueSteps = [2,4]
    plot.yValueAxis.valueMin = 0
    plot.yValueAxis.valueMax = max(y for line in plot.data for _,y in line) * 1.2
    plot.yValueAxis.visibleGrid = True
    plot.yValueAxis.gridStrokeColor = colors.HexColor('#dde3eb')
    plot.xValueAxis.labels.fontName = plot.yValueAxis.labels.fontName = 'Body'
    plot.xValueAxis.labels.fontSize = plot.yValueAxis.labels.fontSize = 9
    for i, color in enumerate(palette):
        plot.lines[i].strokeColor = color
        plot.lines[i].strokeWidth = 1.8
        plot.lines[i].symbol = makeMarker('FilledCircle', size=6, fillColor=color)
    drawing.add(plot)
    drawing.add(String(247, 39, 'Workers per stage', fontName='Body', fontSize=10, textAnchor='middle'))
    drawing.add(String(7, 228, ylabel, fontName='Body', fontSize=9))
    for i,(load,mode) in enumerate(series):
        x=55+(i%2)*205; y=21-(i//2)*14
        drawing.add(Line(x,y+3,x+18,y+3,strokeColor=palette[i],strokeWidth=2))
        drawing.add(String(x+24,y,f'{load} / {mode}',fontName='Body',fontSize=8))
    renderSVG.drawToFile(drawing, str(REPORT/(filename+'.svg')))
    # Render the vector plot through PDFium for a sharp, portable PNG.
    buffer=io.BytesIO();renderPDF.drawToFile(drawing,buffer)
    doc=pdfium.PdfDocument(buffer.getvalue())
    doc[0].render(scale=2).to_pil().save(REPORT/(filename+'.png'))
    doc.close()
    return drawing

chart('elapsed_ms','Completion time: mean of three runs','ms','time')
chart('mean_stats_wait_us','Statistics lock waiting: mean of three runs','us / acquisition','stats_wait')

