# Пайплайн обработки LCA-данных о воздействии продуктов

*English version: [README.md](README.md)*

Читает предоставленный LCA-экспорт (CSV), извлекает **Climate change (кг CO2 экв.)**
и **Water use (м3 экв.)** для восьми логических шагов процесса, перечисленных в
задании, и записывает их в локальную базу SQLite.

## Требования

- Python 3.10 или новее (в коде используется синтаксис типов `X | None`)
- pandas 1.5+

```bash
pip install -r requirements.txt
```

## Запуск

```bash
python run.py                      # берёт data/<предоставленный csv>, пишет в output/lca_data.db
python run.py path/to/other.csv    # явно указанный вход
python run.py --db /tmp/other.db   # явно указанный выход
python run.py -v                   # отладочный вывод в консоль
python run.py --append             # сохранить существующие строки вместо пересоздания
```

По умолчанию каждый запуск пересоздаёт таблицы, поэтому повторный запуск
безопасен и идемпотентен.

## Тесты

```bash
python -m unittest discover -s tests -v
```

25 тестов. Сквозные тесты прогоняют весь пайплайн на предоставленном CSV и
сверяют загруженные значения с исходным файлом; если CSV отсутствует, они
пропускаются автоматически.

Измеренное покрытие по строкам (`python -m coverage run --source=src -m unittest
discover -s tests && python -m coverage report`): **86%**. Непокрытыми остаются
настройка логирования и разбор аргументов командной строки.

## Результаты работы

| Путь | Содержимое |
|---|---|
| `output/lca_data.db` | база SQLite |
| `output/logs/lca_pipeline_<timestamp>.log` | полный лог запуска на уровне DEBUG |

## Модель данных

Две таблицы и одно представление.

**`products`** — одна строка на Product Ref.

| Колонка | Примечания |
|---|---|
| `product_ref` | **первичный ключ**; в задании назван главным идентификатором |
| `product_id` | UUID из исходника |
| `color_code` | |
| `supplier` | заполнен только в строках `PRODUCT_IMPACT`, переносится на продукт |
| `loaded_at` | |

**`product_impacts`** — одна строка на пару (продукт, логический шаг). Всегда
восемь строк на продукт.

| Колонка | Примечания |
|---|---|
| `product_ref` | внешний ключ к `products`, `ON DELETE CASCADE` |
| `process_step` | один из восьми шагов |
| `step_kind` | `LIFECYCLE` (шесть) или `COMPONENT` (два) |
| `climate_change_kg_co2_eq` | `REAL`, допускает NULL |
| `water_use_m3_eq` | `REAL`, допускает NULL |
| `is_present` | `1`, если шаг был в исходнике, `0`, если отсутствовал |
| `loaded_at` | |

Первичный ключ — `(product_ref, process_step)`.

**`v_product_impacts_wide`** — те же данные в широком виде: одна строка на
продукт с шестнадцатью колонками измерений. Именно такую форму подразумевает
описание задания, когда перечисляет восемь шагов рядом. Удобно для быстрого
просмотра:

```sql
SELECT product_ref, main_fabric_co2, linings_co2 FROM v_product_impacts_wide;
```

### Восемь шагов

Шесть берутся из строк с `source = 'PRODUCT_LIFECYCLE_STEP'`, сопоставление по
колонке `Process Step`:

`END_OF_LIFE`, `USE_PHASE`, `DISTRIBUTION`, `WAREHOUSE`, `PRODUCT_TRANSPORT`,
`MANUFACTURING`

Два берутся из строк с `source = 'COMPONENT_IMPACT'`, сопоставление по колонке
`Component category`:

`MAIN_FABRIC`, `LININGS`

Все остальные значения `source` (`COMPONENT_LIFECYCLE_STEP`, `MATERIAL_IMPACT`,
`MATERIAL_LIFECYCLE_STEP`, `PRODUCT_IMPACT`) описывают более детальные или,
наоборот, агрегированные уровни и игнорируются, как того требует задание.

## Результаты на предоставленном CSV

```
прочитано 538 строк, в работу взято 206 (168 lifecycle + 38 component)
28 продуктов
224 строки воздействий (28 x 8)
206 слотов заполнено, 18 пустых
0 предупреждений валидации
```

Все 18 пустых слотов — это `LININGS`: подкладка есть только у 10 из 28 продуктов.
См. раздел про отсутствующие данные ниже.

Каждое загруженное значение сверено с CSV; максимальное расхождение — 0.

## Примеры запросов

```sql
-- один продукт, все восемь шагов
SELECT process_step, step_kind, climate_change_kg_co2_eq, water_use_m3_eq, is_present
FROM product_impacts
WHERE product_ref = '2616093001'
ORDER BY step_kind DESC, process_step;

-- продукты без подкладки
SELECT product_ref FROM product_impacts
WHERE process_step = 'LININGS' AND is_present = 0;

-- суммарный CO2 по восьми шагам, по каждому продукту
SELECT product_ref, ROUND(SUM(climate_change_kg_co2_eq), 4) AS total_co2
FROM product_impacts GROUP BY product_ref ORDER BY total_co2 DESC;
```

```python
import sqlite3, pandas as pd
conn = sqlite3.connect("output/lca_data.db")
df = pd.read_sql("SELECT * FROM v_product_impacts_wide", conn)
```

## Отсутствующие данные

У 18 из 28 продуктов нет компонента `LININGS`. Это свойство самих изделий, а не
дефект выгрузки.

Пайплайн всё равно записывает все восемь строк для каждого продукта: там, где в
исходнике ничего не было, `climate_change_kg_co2_eq` и `water_use_m3_eq`
записываются как `NULL`, а `is_present` — как `0`. Запись `0.0` вместо NULL
сделала бы «у изделия нет подкладки» неотличимым от «у подкладки нулевое
измеренное воздействие», а в данных есть настоящие нули — например, `WAREHOUSE`
равен 0.0 у всех продуктов. Оба случая покрыты тестами.

## Структура проекта

```
run.py                      точка входа
requirements.txt
src/
  config.py                 пути, восемь шагов, имена колонок, настройка логирования
  database.py               схема, upsert-ы, вспомогательные запросы
  transformer.py            разбор CSV, извлечение шагов, построение сетки 8 шагов
  validator.py              структурная валидация, отчёт о покрытии данными
  main.py                   оркестрация и CLI
tests/test_pipeline.py      25 тестов
data/                       входной CSV
output/                     база данных и логи
DECISIONS.md                допущения и технические решения
```

## Диагностика проблем

| Симптом | Причина |
|---|---|
| `TypeError: unsupported operand type(s) for \|` | Python старее 3.10 |
| `CSV not found` (код возврата 2) | неверный путь; укажите его явно |
| `Input rejected: CSV is missing required columns` (код 1) | изменилась структура выгрузки |
| Тесты помечены как `skipped` | CSV отсутствует в каталоге `data/` |
