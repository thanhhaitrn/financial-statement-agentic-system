# AgentFinX — giai đoạn 2 của thí nghiệm an toàn: báo cáo ngắn (25/10/2026)

Báo cáo phục vụ việc viết bài và tái lập kết quả. Mọi con số trong bài lấy từ bảng sinh tự động `artifacts/eval/security/phase2/report-reviewed-20261025/phase2_report.md`, do `backend/analysis/security_phase2/report.py` tạo ra; không số nào được chép tay. Các số trích dưới đây là ảnh chụp của bản đó.

**Khóa ngày 26/10/2026:**
- Mọi bảng của bài (phase 2 và khái quát) được tính lại từ một bộ nhãn cuối chung: `artifacts/eval/security/final-20261026/final_labels.jsonl` (sha256 `bf1a72baf2bb2bc93cc62a1fd1ade96973c6fced8be95eaf42bd385f811a273e`).
  - Nhãn gắn theo từng lượt chạy, tức `(run_dir, run_id)`.
  - Thứ tự ưu tiên: người > AI review > scorer v3.4.
  - Câu nằm ở nhiều gói có đúng một nhãn cuối.
- Bảng đã khóa nằm ở `final-20261026/tables/phase2_report.md`. Mọi số trùng `report-reviewed-20261025`.
- Bản sớm hơn `report-20261025` khác ở Bảng 2c. Bản đó áp 10 quyết định người vào cột nhãn AI, trái mục 3. Số đúng của 2c (κ):
  - v3.3: adopted 0,66, conflict 0,36;
  - v3.4: conflict 0,54.
- Hồ sơ khóa: `docs/security_final/ANALYSIS_RECORD.md` và `LOCK.json`.

## 1. Câu hỏi và phạm vi kết luận

Câu hỏi: khả năng chống chịu của AgentFinX so với RAG thường đến từ đâu?

Trọng tâm của bài là **chuỗi đối chứng khớp evidence A → Apre → Apost → ApostG → D**. Mỗi cấu hình A′ vẫn chỉ gọi model một lần như A, nhưng đưa cho model evidence mà D đã lấy được trong đợt chính:
- Apre: fact BCTC D đã truy xuất và bài báo thô;
- Apost: đầu vào thật của Synth;
- ApostG: như Apost, thêm nhãn untrusted.

Hình chính chỉ gồm các thành phần này (`docs/figures/phase2_evidence_chain_en.svg`; bản tiếng Việt cùng thư mục).

Những điều không được viết:
- Không suy ra hai cấu hình "tương đương" chỉ vì kiểm định không có ý nghĩa. Chỉ viết "không phát hiện khác biệt trên bộ thử này".
- Không diễn giải 0 ca thành công thành miễn nhiễm. Kết quả chỉ nói về bộ tấn công tĩnh đã dùng, và luôn kèm cận trên Clopper–Pearson.
- Không tuyên bố L5 bảo vệ phép tính (xem mục 6).

## 2. Kết quả (nhãn cuối)

**Đợt chính, 80 case (I 40, P 40).**

| | A | B | C | D |
|---|---|---|---|---|
| Tấn công thành công (80) | 38 (+4 chưa xác định) | 1 | 0 | 0 (cận trên CP 4,5%) |
| Câu sạch đúng fact (80) | 41 | 77 | 76 | 78 |
| Tấn công: đúng, không dùng số giả (72) | 27 | 66 | 68 | 66 |

- **A–D:** khác biệt ở cả hai họ. Họ I: 18/0 cặp, Holm p = 3,8·10⁻⁵. Họ P: 20/0 cặp, Holm p = 1,1·10⁻⁵.
- **B–C và C–D:** lệch 0 hoặc 1 cặp. Không phát hiện khác biệt trên bộ thử này.

**Chuỗi A′.** A và D là đúng các câu trả lời của bảng trên.

| | A | Apre | Apost | ApostG | D |
|---|---|---|---|---|---|
| Tấn công thành công (80) | 38 (+4) | 19 (+1) | 6 | 4 | 0 |
| Câu sạch đúng fact (80) | 41 | 75 | 77 | 76 | 78 |
| Tấn công: đúng, không dùng số giả (72) | 37,5% | 73,6% | 87,5% | 88,9% | 91,7% |
| Tấn công: nêu hai nguồn, giữ số BCTC (72) | 19,4% | 40,3% | 62,5% | 61,1% | 41,7% |

So sánh từng bước (McNemar exact, Holm trên 4 kiểm định):

| Bước | Cặp: chỉ trước / chỉ sau | Holm p | Cách viết |
|---|---|---|---|
| A→Apre | 28/7 | 0,002 | có đúng fact BCTC giảm tấn công thành công |
| Apre→Apost | 15/2 | 0,007 | chọn lọc và phân tích tin giảm thêm |
| Apost→D | 6/0 | 0,063 | cùng chiều, không có ý nghĩa sau Holm |
| Apost→ApostG | 3/1 | 0,63 | không quan sát thấy tác dụng của nhãn untrusted trên bộ thử này |

**Phân tích độ nhạy** cho cả hai bảng: coi là adopted mọi câu chỉ có nhãn AI mà luật thô của scorer nói adopted. Hiện không còn câu nào như vậy, vì mọi câu AI và scorer bất đồng đều đã có nhãn người. Bất định còn lại nằm ở các cặp chưa xác định, được báo bằng khoảng:
- A có 4 cặp I3 mà câu sạch cũng không trả lời được;
- Apre có 1 cặp.

**Mẫu số:**
- tỉ lệ tấn công thành công tính trên 80 case;
- hai cột "đúng, không dùng số giả" và "nêu hai nguồn" tính trên 72 case có số giả (bỏ I3);
- độ đúng và từ chối ở câu sạch tính trên 80.

## 3. Quy trình nhãn

1. **Hai AI chấm ban đầu:** Codex và Claude. Phía Claude là các subagent mù, chỉ đọc rubric và câu trả lời.
   - Câu scorer v3.4 tự quyết định được thì giữ nhãn của scorer.
   - Câu scorer để chưa xác định thì được một lượt AI mù chấm, cùng cách cho mọi cấu hình. Gói chấm ẩn tên cấu hình và trộn các cấu hình với nhau.
   - 87 câu trong số đó được Codex chấm lại.
2. **Người kiểm tra và xác nhận nhãn cuối:** thanhhaitrn xác nhận 209 câu trả lời (1.238 nhãn).
   - Phạm vi: mẫu kiểm chứng 110 câu, mọi câu AI và scorer bất đồng, và một mẫu ngẫu nhiên.
   - Cách làm: **không mù**, có xem đối chiếu AI rồi xác nhận hoặc phân xử. Codex nhập liệu theo quyết định của người.
   - Nhãn người được áp dụng cho mọi bảng có câu đó. Ví dụ, H076 và R138 là cùng một câu trả lời, nên có cùng một nhãn.

Trong bài ghi đúng hai bước này. Không mô tả thành hai người chấm mù độc lập.

Bảng 2 tách riêng hai loại số:
- độ đồng ý Codex–Claude: giữa hai AI;
- độ khớp scorer–nhãn người, với mẫu số là số nhãn người thực có.

Bảng 2c giữ riêng phép so scorer với nhãn AI đã phân xử ban đầu, trước khi áp dụng quyết định của người; không dùng nhãn người cho cột mang tên nhãn AI.

Scorer v3.4 đã được chỉnh trên chính mẫu kiểm chứng: 110 trong 209 câu có nhãn người nằm trong mẫu này. Vì vậy độ khớp của v3.4 **không phải kiểm chứng trên mẫu độc lập**. Ước lượng ít thiên lệch hơn là của v3.3, bản chưa chỉnh: κ adopted 0,65 và conflict 0,40 so với nhãn người, trên các câu v3.3 quyết định được.

## 4. Lỗi dựng replay và cách sửa

- **Lỗi:** trong 17 lượt D, đầu vào Synth lồng thêm một cấp (`retrieval_facts`, `analysis_outputs`). Hàm `replay_worker._facts` khi đó chỉ đọc bảng ở cấp trên cùng. Hậu quả là prompt Apost và ApostG của 17 lượt này không có fact BCTC nào. Kiểm tra trước khi chạy vẫn qua, vì không có gì để so.
- **Sửa:**
  - đọc cấu trúc lồng;
  - thêm kết quả của các agent phân tích, vì đó là một phần đầu vào thật của Synth;
  - kiểm tra trước khi chạy giờ từ chối prompt rỗng fact khi evidence có fact.
- **Chạy lại:** đúng 34 ô (17 lượt × Apost/ApostG) trong `phase2/replay-fix-20261024`, theo protocol `replay_fix_protocol.json`. 446 ô còn lại có prompt trùng từng byte với bản đã chạy nên được giữ nguyên.
- **Nhận định đã rút lại:** trước khi tìm ra lỗi, tôi có viết "khâu cắt của AgentFinX làm rơi fact". Nhận định này sai và đã rút lại; với bản sửa, không có fact cần dùng nào bị rơi.

## 5. Thay đổi so với protocol đã đăng ký (`protocol_phase2.json`)

- **Kiểm chứng nhãn:** protocol đăng ký hai người chấm độc lập. Thực tế là hai AI chấm, AI phân xử, và người kiểm tra không mù để xác nhận nhãn cuối (mục 3).
- **Rubric v3.4:** sửa một lần sau phân xử (mục 6 của rubric), như protocol cho phép. Rubric và scorer v3.4 được đóng băng ở G1 (`backend/benchmarks/security/review/g1_freeze_v3.4.json`). Các quyết định người sau G1 được lưu riêng trong `human_decisions.json`, gồm H076 (`conflict_flagged = false`) và đợt xác nhận ngày 25/10. File phân xử đóng băng vẫn giữ nguyên.
- **Cách gán nhãn:** protocol ghi "chấm bằng scorer v3.4". Thực tế dùng scorer v3.4, cộng AI review cho câu scorer không quyết định được, cộng nhãn người. Cách này áp dụng như nhau cho A–D và A′.
- **Đợt replay:** có thêm một đợt sửa lỗi (mục 4). Đợt này ghim mã sản phẩm `fcf31c1d…` thay vì `3c579b10…` của đợt chính. Phần mã khác nhau là các thay đổi của Việc 3 (`as_structured_fact`, chế độ `structured`, cấu hình E), mà đường replay không chạy tới.
- **ApostG:** được chạy (protocol ghi là tùy chọn).
- **Việc 3 (L5 2×2):** dừng ở G3 (mục 6). Mã của Việc 3 vẫn nằm trong sản phẩm, nên mã sản phẩm hiện tại khác đợt chính.
- **Phân tích đã chốt ngày 23/10** (A 42, B 2) có 6 nhãn tự động sai, chưa ai review. Phân tích này đã được thay bằng nhãn cuối ở mục 2.

## 6. G3 và L5

Pilot L5 có 39/40 ô hợp lệ.
- Fact WEB có cấu trúc được đọc đúng 19/19.
- D chặn ở mọi quyết định.
- Không có phép tính tất định nào chạy (0/20 câu sạch).

Đoạn dùng cho phần giới hạn hoặc phụ lục:

> Pilot G3 không đạt do nhánh tính toán tất định không kích hoạt, kể cả ở câu sạch. Theo quy tắc dừng đã đăng ký, nhóm không triển khai thí nghiệm L5 tiếp theo. Vì vậy, nghiên cứu chưa đánh giá hiệu quả bảo vệ phép tính của L5.

Chẩn đoán offline về khâu ràng buộc toán hạng (chiều `entity` và các nguyên nhân khác) chỉ là ghi chép nội bộ, xem `phase2/l5-pilot-20261024/G3.md`. Không dùng chẩn đoán này để giải thích 0/640 phép tính ở đợt chính: ở đợt chính, 438/480 lượt B/C/D vốn không có hợp đồng tính toán.

## 7. Lượt gọi model

| Đợt | Lượt gọi / trần |
|---|---|
| Đợt chính A–D (`main-20261020`) | 2.919 / 3.600 |
| Controlled L5 (`controlled-L5-20261023`) | 570 / 900 |
| Replay A′ (`phase2/replay-20261024`) | 481 / 550 |
| Replay A′, sửa lỗi 34 ô (`phase2/replay-fix-20261024`) | 35 / 40 |
| Pilot L5 (`phase2/l5-pilot-20261024`) | 154 / 250 |

- Giai đoạn 2 (replay, sửa lỗi, pilot): **670 lượt gọi**, dưới mức 900–1.500 đã ước tính.
- Tất cả các đợt: 4.159 lượt.
- Các bước chấm, review, phân xử và sinh bảng không gọi model của thí nghiệm. AI review chạy bằng subagent, không tính vào ngân sách thí nghiệm.

## 8. Hash của dữ liệu và nhãn dùng cho bảng cuối

Hash của các file đầu vào chính nằm ở mục "Tái lập" của `phase2_report.md` và trường `inputs_sha256` của `phase2_report.json`. Một số file:

| File | SHA-256 |
|---|---|
| `backend/benchmarks/security/RUBRIC_v3.4.md` | `df7c3bbe14ce9998aa43c49ebbce84669dab2cf6145f7a94852f14b8a07a2250` |
| `backend/analysis/security_scorer_v34/scorer.py` | `8ee9bf64b5ce216ec94ecf427f599bec7f6bb9a3cba6df20b0141c785fef6a07` |
| `backend/benchmarks/security/review/g1_freeze_v3.4.json` | `611109a42949c901e8c63d77e4df1fc92be4ebae18f08e36b8aaaf371ee3f7d5` |
| `main-20261020/rescore-v3.4-20261024/scores_main.json` | `72fc4badbc1f4d344d225837dc365a46c5418b4ceece2e19eac56c7a0ff3a3f0` |
| `phase2/replay-20261024/manifest.json` | `c5256d004e355d93d39db286c0c79eb3db9de00c014db480fc07ea412ac7b98d` |
| `phase2/replay-fix-20261024/manifest.json` | `583820063bb7d309454c515b77420941f073a1b50c73fd4db65a701442730290` |
| `phase2/human-check-20261025/human_review.json` | `6ebd640599b8ad36cf546c47aff0755b385ba67686a32799ec017297d433f211` |

Các đường dẫn tính từ `artifacts/eval/security/`, trừ các file `backend/…`.

## 9. Tái lập (không gọi model, trừ các lệnh `run`)

Chạy từ `backend/`, trừ khi có ghi chú khác:

```sh
# Chấm lại đợt chính bằng v3.4 (chạy từ thư mục gốc repo, dùng runtime đã đóng băng)
RT=artifacts/eval/security/main-20261020/analysis-runtime-7d5d5817
FINX_HOME="$PWD" PYTHONPATH="$PWD/$RT/backend/src" .venv/bin/python -m backend.analysis.security_scorer_v34.rescore \
  --source artifacts/eval/security/main-20261020 --out <thư mục mới> --frozen-benchmarks $RT/backend/benchmarks

# Evidence của D (cổng G2), replay và đợt sửa lỗi
agentfinx eval-security replay-extract --out <thư mục mới>
agentfinx eval-security run --protocol benchmarks/security/replay_protocol.json --out <dir> --allow-live
agentfinx eval-security run --protocol benchmarks/security/replay_fix_protocol.json --out <dir> --allow-live

# Gói review cho câu v3.4 chưa xác định
python -m analysis.security_scorer_v34.ambiguous_review build|slices|record ...

# Bảng 1–3 từ nhãn cuối, và hình chính
python -m analysis.security_phase2.report --root ../artifacts/eval/security --out <thư mục mới>
python -m analysis.security_phase2.figure --lang en --out ../docs/figures/phase2_evidence_chain_en.svg
```

Script báo cáo dừng khi còn câu chưa xác định mà chưa có review, hoặc khi hai nhãn người cho cùng một câu mâu thuẫn nhau.

## 10. Hạn chế

- **Nhãn người không mù:** người kiểm tra đã xem nhãn AI trước khi xác nhận.
- **Scorer v3.4 không được kiểm chứng độc lập:** nó đã được chỉnh trên mẫu kiểm chứng.
- **Nhãn người phân bố không đều giữa các cấu hình:** A có 66 câu có nhãn người, D có 17. Lý do là mẫu kiểm chứng lấy mọi câu adopted của đợt chính, mà các câu này chủ yếu thuộc A.
- **Phạm vi hẹp:** một model (gpt-oss:120b, nhiệt độ 0), một lần chạy, bộ tấn công tĩnh 80 case.
- **L5:** chưa đánh giá tác dụng của L5 trên phép tính.
