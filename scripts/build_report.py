"""Build the technical report from measured data and editable sources."""
from pathlib import Path
import textwrap
import re
from xml.sax.saxutils import escape
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                               TableStyle, PageBreak, Preformatted, Image)
from reportlab.graphics.shapes import Drawing, Rect, Line, String, Polygon
from plot import ROOT, REPORT, groups, mean

def table_md(headers, data):
    return '\n'.join(['| '+' | '.join(headers)+' |', '| '+' | '.join(['---']*len(headers))+' |']+
                     ['| '+' | '.join(map(str,row))+' |' for row in data])

time_rows=[]; metric_rows=[]
for key, group in sorted(groups.items()):
    group=sorted(group,key=lambda r:int(r['repeat']))
    label=f'{key[0]} / {key[1]} / {key[2]}'
    time_rows.append([label]+[f"{float(r['elapsed_ms']):.3f}" for r in group]+[f"{mean(key,'elapsed_ms'):.3f}"])
    metric_rows.append([label,group[0]['operations'],f"{mean(key,'mean_stats_wait_us'):.3f}",
                       f"{mean(key,'mean_request_ms'):.3f}",f"{mean(key,'throughput'):.0f}",
                       f"{100*mean(key,'utilization'):.1f}"])

analysis=[]
for load in ('standard','individual'):
    for n in (2,4):
        glob=mean((load,'global',n),'mean_stats_wait_us'); local=mean((load,'stage',n),'mean_stats_wait_us')
        t1=mean((load,'global',n),'elapsed_ms');t2=mean((load,'stage',n),'elapsed_ms')
        analysis.append(f'{load}, {n} потока на ступень: среднее ожидание global {glob:.3f} мкс, '
                        f'stage {local:.3f} мкс; время {t1:.3f} и {t2:.3f} мс соответственно.')

md='''# Лабораторная работа №4
# Взаимное исключение и синхронизация

Дисциплина: ОСИСП  
Вариант 22: Синхронизация статистики  
STUDENT_ID: 109  
Учебное заведение: ____________________  
Выполнил: ____________________  
Группа: ____________________  
Проверил: ____________________  
Год: 2026

## Оглавление

1. Введение, цель и индивидуальное задание
2. Теоретические сведения
3. Архитектура, потоки и ресурсы
4. Алгоритм и синхронизация
5. Windows API и обработка ошибок
6. Инварианты и проверка корректности
7. Методика экспериментов
8. Результаты и графики
9. Анализ и заключение
10. Источники
Приложение Б. Репозиторий Git
Приложение В. Исходники графиков и схем
Приложение Г. Полный исходный код

## 1. Введение, цель и индивидуальное задание

Конкурентное обновление общей статистики требует взаимного исключения. Цель работы -
изучить Critical Section и Semaphore Windows и измерить влияние гранулярности блокировок.
Для варианта 22 реализована трёхступенчатая система со сбором статистики. Сравниваются
одна общая критическая секция статистики и отдельная секция каждой ступени. Исследуются
2 и 4 рабочих потока на каждой ступени. Измеряется ожидание доступа к статистике.

Стандартная нагрузка: BASE_OPERATIONS=500 заявок, SEED=1000. Индивидуальная:
OPERATIONS=500+109*5=1045 заявок; SEED=1000+109*97=11573. Operations означает число
созданных заявок; каждая проходит три обработки. Начальные данные определяются seed
и номером заявки, поэтому вход воспроизводим без зависимости от порядка потоков.

## 2. Теоретические сведения

Состояние гонки возникает при несогласованном обращении к общим данным. Критическая
секция допускает одного владельца и предназначена для потоков одного процесса.
Порядок получения секции не гарантирует FIFO. Семафор хранит число доступных единиц
ресурса. В ограниченной очереди один семафор считает элементы, другой - свободные места.
Чтение и изменение самой очереди дополнительно защищаются критической секцией.

Крупная блокировка упрощает защиту общей структуры, но связывает независимые изменения.
При разделении статистики по ступеням разные ступени могут обновлять разные счётчики
одновременно. Дополнительные блокировки и планирование потоков также имеют стоимость.
Увеличение параллелизма само по себе не гарантирует ускорения.

## 3. Архитектура, потоки и ресурсы

Один процесс содержит Generator в основном потоке, 3*N рабочих потоков и Collector.
При N=2 это 8 потоков всего, при N=4 - 14. Каждый участник получает уникальный номер:
Generator=0, рабочие=1..3N, Collector=3N+1. Журнал содержит PID и TID.
Между соседними участниками находятся четыре ограниченные FIFO-очереди.

```text
Generator -> Q0 -> Stage1[N] -> Q1 -> Stage2[N] -> Q2 -> Stage3[N] -> Q3 -> Collector
```

В каждой очереди: deque<Job>, Critical Section, Semaphore items, Semaphore spaces.
Ёмкость по умолчанию 32. Очереди не зависят от выбранного режима статистики.
Общие данные: очереди, три StageStats, состояния заявок, результаты, Event отмены.
Local каждого рабочего хранит собственные метрики и не требует блокировки. Collector
единолично записывает результаты; основной поток читает их только после join.

Структура Job содержит id, value, временные метки и накопленное ожидание. Отрицательный
id обозначает служебный элемент остановки. Каждая ступень преобразует value ограниченным
целочисленным вычислением. В StageStats хранятся count и checksum. Итоговая статистика
читается основным потоком после завершения всех рабочих, поэтому снимок согласован.

## 4. Алгоритм и синхронизация

Producer ожидает spaces, добавляет элемент под CS очереди и увеличивает items.
Consumer ожидает items, извлекает элемент под CS и увеличивает spaces. Ожидание
семафоров выполняется вне CS, поэтому поток не удерживает очередь, ожидая партнёра.
Очередь FIFO определяет порядок извлечения; завершения нескольких рабочих могут
происходить в другом порядке, результаты связываются по id.

После вычисления рабочий выбирает globalStats либо stageLocks[stage], измеряет время
до и после EnterCriticalSection, обновляет count и checksum, затем освобождает секцию.
В обоих режимах выполняется одинаковая работа. Параметр stats-work=2000 задаёт число
шагов синтетического обновления контрольной суммы внутри секции. Это управляемая
нагрузка на статистику, а не обязательная стоимость обычного инкремента. Параметр
work=20000 задаёт шаги обработки заявки вне секции. Sleep для согласования не применяется.

Основные фрагменты реализации:

```cpp
auto t = counter();
c.wait(spaces.h);
{ Guard guard(lock); jobs.push_back(job); }
release(items.h);
```

```cpp
CS& lock = c.cfg.separate ? c.stageLocks[l.stage] : c.globalStats;
auto before = counter();
{
    Guard guard(lock);
    auto acquired = counter();
    l.statsWait += c.ms(acquired-before);
    auto& stat = c.stats[l.stage];
    ++stat.count;
    // bounded checksum update
}
```

Штатное завершение: Generator помещает N STOP в Q0 и ожидает первую группу. Затем
помещает N STOP в Q1 и ожидает вторую, аналогично третью. После третьей ступени
в Q3 помещается один STOP для Collector. STOP добавляется только после завершения
предыдущей группы, поэтому в очереди не появятся новые данные после служебных элементов.

Вложенного удержания секций очереди и статистики нет. После отмены все ожидания
семафоров просыпаются по manual-reset Event. Все потоки присоединяются перед удалением
общих объектов, в том числе если создание части рабочих не удалось.

## 5. Windows API и обработка ошибок

| API | Назначение |
|---|---|
| CreateThread | Запуск рабочих и сборщика |
| InitializeCriticalSectionEx | Инициализация CS |
| EnterCriticalSection / LeaveCriticalSection | Взаимное исключение |
| DeleteCriticalSection | Освобождение CS |
| CreateSemaphoreW / ReleaseSemaphore | Учёт элементов и мест |
| CreateEventW / SetEvent | Общая отмена |
| WaitForMultipleObjects | Ожидание ресурса или отмены с дедлайном |
| WaitForSingleObject | Присоединение завершающихся потоков |
| InterlockedCompareExchange | Атомарный контроль состояния заявки |
| QueryPerformanceCounter / Frequency | Измерение времени |
| GetCurrentProcessId / GetCurrentThreadId | Идентификация участников |
| GetLastError / FormatMessageA / LocalFree | Диагностика |
| CloseHandle | Закрытие HANDLE |

Проверяются результаты вызовов, возвращающих статус. При системной ошибке выводится
диагностика; ввод проверяется до создания участников. Все HANDLE и CS освобождаются
объектами RAII. Исключение рабочего устанавливает отмену и приводит к коду 1.
TerminateThread не применяется. Указанное max-ms ограничивает моделирование; при
отмене допускается короткое превышение на завершение текущей ограниченной операции
и join. Дедлайн не включает последующую последовательную проверку результатов.
EnterCriticalSection не имеет тайм-аута: код секций конечен, не ожидает других ресурсов,
а параметры ограничены. Это предотвращает алгоритмическое зависание в модели;
пауза самого процесса средствами ОС не может быть ограничена таким дедлайном.

## 6. Инварианты и проверка корректности

Проверяются отсутствие переполнения/опустошения очереди, допустимость id и однократность
каждой обработки. Состояния заявки: 0 -> 1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7, где нечётные
1,3,5 означают обработку на соответствующей ступени, 2,4,6 - завершение ступени,
7 - получение Collector. Переходы выполняются InterlockedCompareExchange с проверкой
предыдущего состояния. Дубликат либо нарушение порядка вызывает ошибку.

После join проверяются: completed=operations, все состояния равны 7, пустые очереди,
count каждой ступени равен operations, все результаты и суммы checksum совпадают
с независимым последовательным повтором того же вычисления. Число нарушений,
ошибок и тайм-аутов выводится в SUMMARY. Успешные тесты дополняют обоснование
синхронизации, но не доказывают отсутствие любой возможной ошибки.

Девять тестов tests/test.ps1: режимы global/stage при capacity=1, одна заявка при
четырёх рабочих, индивидуальная нагрузка, управляемый отказ рабочего на id=10,
дедлайн 10 мс при тяжёлой нагрузке, нулевое число рабочих, неверный режим,
неизвестный параметр. Все тесты прошли. В тесте дедлайна зафиксирована отмена без
потери управления; в тесте отказа остальные потоки завершились через Event.
Журналы: data/tests/*.txt, итог: data/tests/results.csv. Подробный демонстрационный
журнал 12 заявок - data/demo.txt; он содержит WAITING_QUEUE, WORKING, WAITING_STATS,
RELEASED_STATS, COLLECTED, FINISHED и идентификаторы заявок.

## 7. Методика экспериментов

Эксперименты выполнены 10.11.2026: Windows 10.0.19045 x64, Intel64 Family 6 Model 42,
MinGW GCC 15.2.0, C++17, оптимизация O2. Для каждой комбинации нагрузки, режима
и числа рабочих выполнены три отдельных запуска: всего 24. Подробный журнал отключён.
Порядок global/stage меняется во втором повторе. Очереди capacity=32, work=20000,
stats-work=2000, max-ms=30000. Все параметры доступны в CLI.

elapsed_ms включает создание потоков, обработку и штатный join; создание очередей и
последовательная проверка после join исключены. Пропускная способность - завершённые
заявки в секунду. mean_stats_wait_us - сумма ожиданий секции статистики, делённая на
3*operations, в микросекундах. Измерение охватывает накладные расходы входа в секцию
и планирования. Время печати подробных событий отсутствует, накладные расходы вызовов
журнала и измерений остаются одинаковыми в двух режимах.

mean_request_ms - средняя длительность от первой попытки постановки до Collector;
mean_request_wait_ms - суммарное ожидание свободного места и пребывание в очередях
на одну заявку. queue_wait_ms отдельно суммирует блокирующие ожидания рабочих
на семафорах; эти показатели имеют разный смысл. processing_ms измеряет вычисление
вне статистики. utilization - доля времени занятости рабочих от извлечения до
завершения обновления статистики, включая ожидание её CS; это не загрузка CPU.
Суммы времён разных потоков могут превышать elapsed_ms.

## 8. Результаты и графики

В обозначениях ниже standard=500 заявок, individual=1045, global=одна CS,
stage=три CS. Последнее число в конфигурации - число рабочих на ступень.

'''
md += table_md(['Конфигурация','Запуск 1, мс','Запуск 2, мс','Запуск 3, мс','Среднее, мс'],time_rows)
md += '\n\nПрофильные показатели (среднее трёх запусков):\n\n'
md += table_md(['Конфигурация','Завершено','CS ожид., мкс','Заявка, мс','Заявок/с','Занятость, %'],metric_rows)
md += '''

Во всех 24 запусках rejected=0, violations=0, errors=0, timeouts=0.
Полные исходные данные со всеми метриками: data/experiments.csv и data/runs/*.txt.

![График 1. Время выполнения](time.png)

График 1 - среднее время выполнения при изменении числа рабочих на ступень.
Линии соединяют только измеренные значения 2 и 4, интерполяция не исследована.

![График 2. Ожидание секции статистики](stats_wait.png)

График 2 - среднее ожидание одного доступа к статистике. Исходники обоих графиков:
scripts/plot.py; редактируемые векторные версии: report/time.svg,
report/stats_wait.svg. Три повтора приведены в таблице и исходном CSV; графики
показывают только средние и не являются доверительными интервалами.

## 9. Анализ и заключение

'''+ '\n\n'.join(analysis)+'''

Ожидалось снижение межступенчатой конкуренции при отдельных CS. Результат зависит
от числа потоков и нагрузки: на коротких запусках влияет планировщик, создание
потоков и загрузка компьютера. Поэтому отдельные CS не следует считать гарантией
ускорения каждого запуска. Четыре рабочих на ступень увеличивают конкуренцию внутри
ступени, а общий объём вычислений и накладные расходы ограничивают масштабирование.
Полученные средние характеризуют эти параметры и синтетическую стоимость статистики;
они не переносятся автоматически на реальное приложение с простым счётчиком.

Разработана трёхступенчатая модель Windows с двумя способами защиты статистики.
Семафоры обеспечивают ограничение очередей, критические секции защищают их содержимое
и статистику. Выполнены стандартные и индивидуальные эксперименты, построены два
графика по собственным данным. Все предусмотренные нормальные запуски завершились
без нарушений инвариантов; проверены отказ рабочего и ограничение времени.

## 10. Источники

1. Задание лабораторной работы, предоставленное пользователем, вариант 22.
2. Microsoft Learn. Critical Section Objects.
https://learn.microsoft.com/en-us/windows/win32/sync/critical-section-objects
3. Microsoft Learn. Semaphore Objects.
https://learn.microsoft.com/en-us/windows/win32/sync/semaphore-objects
4. Microsoft Learn. WaitForMultipleObjects.
https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-waitformultipleobjects
5. Microsoft Learn. Acquiring high-resolution time stamps.
https://learn.microsoft.com/en-us/windows/win32/sysinfo/acquiring-high-resolution-time-stamps

## Приложение Б. Репозиторий Git

Репозиторий: https://github.com/Bahemaal/OSISP
Ветка: main. Разработка велась с первого этапа в локальном Git. Адрес удалённого
репозитория предоставлен после первых этапов; первые три коммита опубликованы вместе.
Это не имитация разнесённых по времени отправок контрольных версий. Сроки преподавателя
и предоставление ему доступа студент согласует самостоятельно.

Контрольные коммиты:
e01da09 - проектирование структуры и архитектуры.
1ea0323 - реализация очередей, потоков и двух режимов статистики.
a6b84ef - инварианты, отмена, тесты и 24 измерения.

Окончательный технический комплект зафиксирован именованной версией lab04-v1.
Полный SHA указан в файле FINAL_COMMIT.txt рядом с отчётом после фиксации версии.
Сам отчёт не может содержать SHA коммита, который ещё включает его собственное
содержимое: после создания версии SHA записывается отдельным сопровождающим файлом.
Для представления со всеми личными приложениями следует создать следующую версию
и указать её фактический SHA в сопроводительных сведениях.

## Приложение В. Исходники графиков и схем

Построение графиков выполняется ReportLab LinePlot. PNG получены рендерингом
векторного рисунка через PDFium. Воспроизведение:

```text
python -m pip install reportlab pypdfium2
python scripts/plot.py
python scripts/build_report.py
```

Схема взаимодействия описана в Mermaid, файл diagrams/pipeline.mmd.

'''
md += '```mermaid\n'+(ROOT/'diagrams/pipeline.mmd').read_text(encoding='utf-8')+'```\n\n'
# Include the generator itself by reference in MD, and a full listing in the PDF.
md += 'Полный листинг скрипта графиков приведён в PDF и находится в scripts/plot.py. Сборка отчёта: scripts/build_report.py.\n\n'
md += '## Приложение Г. Полный исходный код\n\n```cpp\n'+(ROOT/'src/main.cpp').read_text(encoding='utf-8-sig')+'```\n'
(REPORT/'REPORT.md').write_text(md,encoding='utf-8')

styles=getSampleStyleSheet()
styles.add(ParagraphStyle(name='P',fontName='Body',fontSize=10,leading=14,spaceAfter=7))
styles.add(ParagraphStyle(name='H',fontName='Bold',fontSize=14,leading=18,spaceBefore=13,spaceAfter=9,keepWithNext=True))
styles.add(ParagraphStyle(name='T',fontName='Bold',fontSize=23,leading=29,alignment=TA_CENTER,spaceAfter=14))
styles.add(ParagraphStyle(name='Mono',fontName='Code',fontSize=6.7,leading=8.6,spaceAfter=8))
styles.add(ParagraphStyle(name='Cell',fontName='Body',fontSize=7.3,leading=10))
story=[Spacer(1,105)]
def paragraph(s,style='P'):
    return Paragraph(escape(s),styles[style])
def add_table(lines):
    data=[]
    for line in lines:
        fields=[x.strip() for x in line.strip().strip('|').split('|')]
        if all(x.replace('-','').replace(':','')=='' for x in fields):continue
        data.append([paragraph(x,'Cell') for x in fields])
    count=len(data[0]); widths=[166]+[(470-166)/(count-1)]*(count-1) if count>2 else [236,234]
    table=Table(data,colWidths=widths,repeatRows=1,hAlign='LEFT')
    table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#dce6f2')),
                              ('VALIGN',(0,0),(-1,-1),'TOP'),('GRID',(0,0),(-1,-1),0.3,colors.HexColor('#bac4d0')),
                              ('BOTTOMPADDING',(0,0),(-1,-1),6),('TOPPADDING',(0,0),(-1,-1),6)]))
    story.extend([table,Spacer(1,9)])

lines=md.splitlines();i=0
while i<len(lines):
    line=lines[i]
    if line.startswith('# '):
        story.append(paragraph(line[2:],'T'));i+=1;continue
    if line.startswith('## '):
        if line.startswith('## Оглавление') or line.startswith('## 1.') or line.startswith('## Приложение'):
            story.append(PageBreak())
        story.append(paragraph(line[3:],'H'));i+=1;continue
    if line.startswith('```'):
        block=[];i+=1
        while i<len(lines) and not lines[i].startswith('```'):
            block+=textwrap.wrap(lines[i],width=112,replace_whitespace=False,drop_whitespace=False) or ['']
            i+=1
        if block and block[0].startswith('Generator ->'):
            diagram=Drawing(470,85)
            labels=['Generator','Q0','Stage 1','Q1','Stage 2','Q2','Stage 3','Q3','Collector']
            for n,label in enumerate(labels):
                x=n*52
                diagram.add(Rect(x,35,45,27,fillColor=colors.HexColor('#e3edf8'),strokeColor=colors.HexColor('#6984a6')))
                diagram.add(String(x+22.5,46,label,fontName='Body',fontSize=6.8,textAnchor='middle'))
                if n<8:
                    diagram.add(Line(x+45,48,x+51,48,strokeColor=colors.HexColor('#6984a6')))
                    diagram.add(Polygon([x+51,48,x+48,50,x+48,46],fillColor=colors.HexColor('#6984a6')))
                if n in (2,4,6):diagram.add(String(x+22.5,22,'N workers',fontName='Body',fontSize=6.7,textAnchor='middle'))
                if n in (1,3,5,7):diagram.add(String(x+22.5,22,'CS + Sem',fontName='Body',fontSize=6.7,textAnchor='middle'))
            story.append(diagram)
        else:
            story.append(Preformatted('\n'.join(block),styles['Mono']))
        i+=1;continue
    if line.startswith('|'):
        block=[]
        while i<len(lines) and lines[i].startswith('|'):block.append(lines[i]);i+=1
        add_table(block);continue
    if line.startswith('!['):
        filename=line.split('](')[1].rstrip(')')
        story.extend([Image(str(REPORT/filename),width=470,height=265),Spacer(1,8)]);i+=1;continue
    if not line.strip():i+=1;continue
    if re.match(r'^\d+\.\s',line) or line.startswith('Приложение '):
        story.append(paragraph(line));i+=1;continue
    block=[line];i+=1
    while i<len(lines) and lines[i].strip() and not lines[i].startswith(('#','|','```','![')):
        block.append(lines[i]);i+=1
    if any(x.endswith('  ') for x in block):
        cover=ParagraphStyle(name='Cover',parent=styles['P'],alignment=TA_CENTER,leading=21)
        story.extend([Spacer(1,28),Paragraph('<br/>'.join(escape(x.strip()) for x in block),cover)])
    else:
        story.append(paragraph(' '.join(block)))

story.append(PageBreak());story.append(paragraph('Листинг скрипта графических материалов','H'))
source=(ROOT/'scripts/plot.py').read_text(encoding='utf-8')
wrapped=[]
for line in source.splitlines():wrapped+=textwrap.wrap(line,width=112,replace_whitespace=False,drop_whitespace=False) or ['']
story.append(Preformatted('\n'.join(wrapped),styles['Mono']))

def footer(canvas,doc):
    canvas.saveState();canvas.setFont('Body',8);canvas.setFillColor(colors.HexColor('#667085'))
    canvas.drawString(62,28,'ОСИСП / ЛР4 / Вариант 22 / STUDENT_ID 109')
    canvas.drawRightString(533,28,str(doc.page));canvas.restoreState()

SimpleDocTemplate(str(REPORT/'REPORT.pdf'),pagesize=(595.28,841.89),leftMargin=62,rightMargin=62,
                  topMargin=48,bottomMargin=47,title='ОСИСП. ЛР4. Вариант 22',author='').build(story,onFirstPage=footer,onLaterPages=footer)
print('Built REPORT.md, REPORT.pdf and two measured charts')
