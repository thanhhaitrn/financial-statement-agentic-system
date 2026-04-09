# Huong dan su dung `test.py`

`test.py` la CLI de chay pipeline agentic financial QA tren mot dataset da dang ky hoac mot file tai lieu moi.

## Yeu cau

- Chay lenh trong thu muc project: `/Users/thanhhai/itec`
- Moi truong Python va dependencies cua repo da duoc cai dat

## Cach chay nhanh

Chay voi dataset mac dinh (document.md):

```bash
python test.py --query "ROE quy 2/2025 la bao nhieu?"
```

Neu chay tu terminal interactive ma khong truyen `--query`, chuong trinh se hoi:

```bash
python test.py
```

## Dataset duoc chon nhu the nao

`test.py` resolve dataset theo thu tu sau:

1. Neu co `--file-path`: tao hoac cap nhat dataset tu file nay.
2. Neu co `--dataset-id`: lay dung dataset theo id.
3. Neu co bo loc nhu `--company`, `--ticker`, `--fiscal-year`...: tim dataset trong registry.
4. Neu dang chay interactive va co nhieu dataset phu hop: yeu cau chon dataset.
5. Neu khong truyen bo loc nao va registry chua co du lieu phu hop: tu dong dung dataset mac dinh trong `data/document.md`.

Registry dataset nam o `dataset_store/registry.json`.

## Cac lenh hay dung

Liet ke dataset da dang ky:

```bash
python test.py --list-datasets
```

Chay batch tren tat ca dataset da dang ky va ghi output ra JSON:

```bash
python dataset_batch_runner.py \
  --query "ROE là bao nhiêu?" \
  --output batch_test_results.json
```

Mac dinh, neu file output da ton tai, script se merge them ket qua theo tung `query` vao cung file JSON. Query moi se duoc them vao `query_reports`; neu chay lai cung mot query, ket qua query do se duoc cap nhat. Neu muon ghi de hoan toan, them `--overwrite-output`.

De cap nhat trace cho 1 dataset cu the trong file batch JSON:

```bash
python dataset_batch_runner.py \
  --dataset-id hoaphat \
  --query "Biên lợi nhuận ròng của công ty là bao nhiêu?" \
  --output batch_test_results.json \
  --include-trace
```

Voi `--dataset-id`, script chi chay cac dataset duoc chi dinh va khi merge se chi cap nhat ket qua cua cac dataset do trong `query_report`, khong xoa ket qua cu cua dataset khac.

Xoa mot dataset da dang ky theo id:

```bash
python test.py --delete-dataset --dataset-id song-da-financial-statement-2024-unknown-unknown
```

Chay voi dataset theo id:

```bash
python test.py --dataset-id hoa-phat-financial-statement-2025-q2-consolidated-unaudited --query "Loi nhuan sau thue la bao nhieu?"
```

Loc dataset theo metadata:

```bash
python test.py --company "Hoa Phat" --fiscal-year 2025 --fiscal-quarter 2 --scope consolidated --query "ROE la bao nhieu?"
```

Bat buoc chon thu cong trong cac dataset match:

```bash
python test.py --company "Hoa Phat" --select-dataset
```

Dang ky dataset moi tu file:

```bash
python test.py \
  --file-path data/document.md \
  --company "Cong ty Co phan Song Da" \
  --fiscal-year 2024 \
  --query "Tong tai san la bao nhieu?"
```

`--file-path` co the la duong dan tuong doi hoac tuyet doi. Neu la duong dan tuong doi, no se duoc resolve tu root cua repo.
Neu khong truyen `--company`, CLI se khong con fallback sang cong ty mac dinh. Neu ban cung khong truyen `--dataset-id`, he thong se dung ten file lam `dataset_id` tam.

## Cac tham so chinh

- `--list-datasets`: in danh sach dataset va thoat
- `--delete-dataset`: xoa dataset da dang ky va thoat
- `--yes`: bo qua prompt xac nhan cho thao tac pha huy nhu xoa dataset
- `--dataset-id`: chon dataset da ton tai theo id
- `--select-dataset`: bat prompt chon dataset trong cac ket qua match
- `--company`
- `--ticker`
- `--industry`
- `--report-type`
- `--fiscal-year`
- `--fiscal-quarter`
- `--scope`
- `--audit-status`
- `--file-path`: dang ky/build dataset tu file tai lieu
- `--query`: chay query khong tuong tac
- `--debug-trace`: hien them log trace noi bo

## Batch test JSON

Script [dataset_batch_runner.py](/Users/thanhhai/itec/dataset_batch_runner.py) chay qua toan bo dataset hien co trong registry, thuc thi mot hoac nhieu cau hoi, roi luu ket qua vao file JSON.

Vi du 1 cau hoi:

```bash
python dataset_batch_runner.py \
  --query "ROE là bao nhiêu?" \
  --output batch_test_results.json
```

Vi du nhieu cau hoi:

```bash
python dataset_batch_runner.py \
  --query "ROE là bao nhiêu?" \
  --query "Lợi nhuận sau thuế là bao nhiêu?" \
  --output batch_test_results.json
```

Vi du doc cau hoi tu file:

```bash
python dataset_batch_runner.py \
  --queries-file queries.txt \
  --output batch_test_results.json
```

`queries.txt` co the la file text moi dong 1 cau hoi, hoac file `.json` dang list:

```json
[
  "ROE là bao nhiêu?",
  "Lợi nhuận sau thuế là bao nhiêu?"
]
```

Trong JSON output, top-level se co `query_reports`. Moi `query_report` ung voi 1 cau hoi, va ben trong moi dataset co `run` cua query do.

Moi `run` co:

- `query`
- `formatted_answer`
- `answer`
- `synth_status`
- `errors`
- `run_summary`

Neu muon luu ca trace chi tiet, them `--include-trace`. Neu muon bat trace debug cho pipeline, them `--debug-trace`.

## Trace va debug

Mac dinh, trace chi giu lai cac event chinh de de nhin va de visualize.

Bat debug trace:

```bash
python test.py --debug-trace --query "ROE quy 2/2025 la bao nhieu?"
```

Khi bat `--debug-trace`, cac event noi bo hon se duoc in them.

## Xoa dataset

`--delete-dataset` chi xoa dataset da ton tai. Lenh nay khong bao gio:

- tu dong tao dataset mac dinh
- dang ky dataset moi tu `--file-path`
- xoa source document goc

Lenh xoa se luon:

- xoa dataset khoi `dataset_store/registry.json`
- xoa manifest cua dataset
- xoa SQLite DB cua dataset
- xoa file raw tables
- xoa vector collection trong Chroma

Neu dang chay interactive, CLI se yeu cau ban go `DELETE` de xac nhan. Neu chay khong interactive, can them `--yes`.

## Output co y nghia gi

Moi lan chay se in 3 phan:

```text
=== DATASET ===
...

=== TRACE ===
{'event': 'planner:done', ...}
...
{'event': 'run:done', 'duration_ms': ..., 'input_tokens': ..., 'output_tokens': ..., 'total_tokens': ...}

=== FINAL ANSWER ===
ANSWER: ...
```

- `DATASET`: dataset dang duoc dung
- `TRACE`: log live theo tung node trong luc graph dang chay
- `run:done`: tong hop thoi gian chay va tong token cua ca run
- `FINAL ANSWER`: cau tra loi da duoc format

Neu pipeline gap loi, chuong trinh se in them `=== ERROR SUMMARY ===` ra `stderr` va thoat voi exit code `1`.

## Luu y khi chay lan dau

Khi dataset chua duoc build, `test.py` se tu dong:

1. Tao knowledge base SQLite
2. Build vector store
3. Cap nhat manifest/registry cua dataset

Vi vay, lan chay dau voi mot dataset moi co the cham hon cac lan sau.

## Goi y su dung

- Dung `--list-datasets` de kiem tra registry truoc khi query
- Dung `--dataset-id` neu muon chay on dinh trong script hoac CI
- Dung `--debug-trace` khi can debug flow agent/tool/synth
- Dung `--file-path` khi muon nap nhanh mot bao cao moi vao he thong
