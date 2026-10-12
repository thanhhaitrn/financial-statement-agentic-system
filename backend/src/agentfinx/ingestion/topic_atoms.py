"""Derive small, reusable topic facts from financial-report narrative."""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class TopicAtom:
    """A focused fact derived from a paragraph that remains stored verbatim."""

    topic: str
    value: str
    atom_type: str = "topic"
    metric_label: str = ""
    entity_label: str = ""


_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+(?=(?:[\"'“‘(\[]*[A-ZÀ-ỸĐ0-9]))")
_DURATION_RE = re.compile(
    r"\b(?:khấu\s+hao|hao\s+mòn|phân\s+bổ)\b.*?"
    r"(?:từ\s+)?\d+(?:[.,]\d+)?\s*(?:[-–—]\s*|đến\s+)?"
    r"(?:\d+(?:[.,]\d+)?\s*)?năm",
    flags=re.IGNORECASE,
)
_CONDITION_RE = re.compile(
    r"(?:\bnếu\b|(?<!sau\s)\bkhi\b|\btrừ\s+khi\b|"
    r"\bchỉ\s+được\s+(?:ghi\s+nhận|ghi\s+tăng|vốn\s+h[oó]a)\b|"
    r"trong\s+trường\s+hợp|điều\s+kiện|thỏa\s+mãn|thoả\s+mãn|"
    r"có\s+thể\s+chứng\s+minh|chắc\s+chắn)",
    flags=re.IGNORECASE,
)
_RECOGNITION_RE = re.compile(
    r"\b(?:ghi\s+nhận|ghi\s+tăng\s+nguyên\s+giá)\b",
    flags=re.IGNORECASE,
)
_CAPITALIZATION_RE = re.compile(r"\bvốn\s+h[oó]a\b", flags=re.IGNORECASE)
_LISTING_RE = re.compile(
    r"(?:"
    r"\bmã\s+(?:chứng\s+khoán|cổ\s+phiếu)\b|"
    r"\b(?:cổ\s+phiếu|chứng\s+khoán)\s+của\s+(?:công\s+ty\b)?.{0,180}"
    r"(?:niêm\s+yết|đăng\s+ký\s+giao\s+dịch|upcom|hose|hnx|hsx|"
    r"sở\s+giao\s+dịch\s+chứng\s+khoán)|"
    r"\b(?:cổ\s+phiếu|chứng\s+khoán)\b.{0,100}\bđược\s+"
    r"(?:niêm\s+yết|đăng\s+ký\s+giao\s+dịch)\b"
    r")",
    flags=re.IGNORECASE,
)
_TICKER_RE = re.compile(
    r"\bmã\s+(?:chứng\s+khoán|cổ\s+phiếu)\s*"
    r"(?:[:\-]\s*|\blà\s+)?(?P<ticker>[A-Z][A-Z0-9]{1,9})\b",
    flags=re.IGNORECASE,
)
_DEPRECIATION_METHOD_RE = re.compile(
    r"\b(?:khấu\s+hao|hao\s+mòn)\b.{0,180}?"
    r"\b(?:phương\s+pháp\s+)?(?:đường\s+thẳng|số\s+dư\s+giảm\s+dần|"
    r"sản\s+lượng|theo\s+số\s+lượng\s+sản\s+phẩm)\b",
    flags=re.IGNORECASE,
)
_AMORTIZATION_METHOD_RE = re.compile(
    r"\bphân\s+bổ\b.{0,180}?"
    r"\b(?:phương\s+pháp\s+)?(?:đường\s+thẳng|theo\s+thời\s+gian|"
    r"theo\s+sản\s+lượng)\b",
    flags=re.IGNORECASE,
)
_NON_DEPRECIATION_RE = re.compile(
    r"\b(?:không|chưa)\s+(?:được\s+)?(?:tính\s+|trích\s+)?"
    r"(?:khấu\s+hao|hao\s+mòn)\b",
    flags=re.IGNORECASE,
)
_MEASUREMENT_BASIS_RE = re.compile(
    r"\b(?:ghi\s+nhận|xác\s+định|đo\s+lường|trình\s+bày)\b.{0,160}?"
    r"\b(?:giá\s+gốc|giá\s+trị\s+hợp\s+lý|giá\s+trị\s+thuần\s+"
    r"có\s+thể\s+thực\s+hiện|giá\s+trị\s+còn\s+lại)\b",
    flags=re.IGNORECASE,
)
_PERSON_EVENT_RE = re.compile(
    r"\b(?:"
    r"bổ\s+nhiệm|được\s+bầu|bầu\s+làm|"
    r"miễn\s+nhiệm|từ\s+nhiệm|thôi\s+giữ\s+chức|"
    r"chấm\s+dứt\s+(?:nhiệm\s+kỳ|hợp\s+đồng)|"
    r"thay\s+đổi\s+(?:thành\s+viên|nhân\s+sự)"
    r")\b",
    flags=re.IGNORECASE,
)
_APPOINTMENT_RE = re.compile(
    r"\b(?:bổ\s+nhiệm|được\s+bầu|bầu\s+làm)\b",
    flags=re.IGNORECASE,
)
_DEPARTURE_RE = re.compile(
    r"\b(?:miễn\s+nhiệm|từ\s+nhiệm|thôi\s+giữ\s+chức|"
    r"chấm\s+dứt\s+(?:nhiệm\s+kỳ|hợp\s+đồng))\b",
    flags=re.IGNORECASE,
)
_PERSON_NAME_RE = re.compile(
    r"\b(?:Ông|Bà|Mr|Mrs|Ms)\.?\s+"
    r"(?P<name>"
    r"[A-ZÀ-ỸĐ][A-Za-zÀ-ỹĐđ'’-]*"
    r"(?:\s+[A-ZÀ-ỸĐ][A-Za-zÀ-ỹĐđ'’-]*){1,6}"
    r")"
    r"(?=\s+(?:được|bị|đã|bổ|miễn|từ|thôi|làm|giữ|chức|sẽ)\b|[,.;])"
)
_PERSON_NAME_TOKEN = r"[A-ZÀ-ỸĐ][A-Za-zÀ-ỹĐđ'’.\-]*"
_PERSON_ROLE_NAME_RE = re.compile(
    rf"\b(?P<honorific>(?i:Ông|Bà|Mr|Mrs|Ms))\.?\s+"
    rf"(?P<name>{_PERSON_NAME_TOKEN}"
    rf"(?:\s+{_PERSON_NAME_TOKEN}){{1,7}}?)"
    r"(?=\s*(?:"
    r":|\||[-–—]\s+|"
    r"(?i:"
    r"(?:hiện\s+)?(?:đang\s+)?giữ\s+(?:chức(?:\s+vụ)?)?|"
    r"đảm\s+nhiệm(?:\s+chức(?:\s+vụ)?)?|"
    r"(?:được\s+)?bổ\s+nhiệm|"
    r"làm|là"
    r")\b"
    r"))"
)
_DELIMITED_ROLE_RE = re.compile(
    r"^\s*(?::|\||[-–—])\s*(?P<role>.+?)\s*$",
    flags=re.DOTALL,
)
_PROSE_ROLE_RE = re.compile(
    r"^\s*(?:"
    r"(?:hiện\s+)?(?:đang\s+)?giữ\s+(?:chức(?:\s+vụ)?\s+)?|"
    r"đảm\s+nhiệm(?:\s+chức(?:\s+vụ)?\s+)?|"
    r"(?:được\s+)?bổ\s+nhiệm"
    r"(?:\s+(?:làm|là|giữ\s+chức(?:\s+vụ)?))?\s+|"
    r"(?:làm|là)\s+"
    r")(?P<role>.+?)\s*$",
    flags=re.IGNORECASE | re.DOTALL,
)
_PERSON_ROLE_MARKER_RE = re.compile(
    r"^(?:"
    r"(?:phó\s+)?chủ\s+tịch|"
    r"(?:phó\s+)?tổng\s+giám\s+đốc|"
    r"giám\s+đốc(?:\s+điều\s+hành)?|"
    r"(?:phó\s+)?giám\s+đốc|"
    r"kế\s+toán\s+trưởng|"
    r"(?:phó\s+)?trưởng\s+(?:ban|phòng|bộ\s+phận)|"
    r"thành\s+viên(?:\s+(?:hội\s+đồng|ban))?|"
    r"(?:ủy|uỷ)\s+viên|"
    r"kiểm\s+soát\s+viên|"
    r"kiểm\s+toán\s+viên|"
    r"người\s+đại\s+diện(?:\s+theo\s+pháp\s+luật)?|"
    r"chief\s+\w+|(?:managing|executive|finance)\s+director|"
    r"chair(?:man|woman|person)?|director"
    r")\b",
    flags=re.IGNORECASE,
)
_ROLE_QUALIFIER_RE = re.compile(
    r"(?:\s*\|\s*|\s*,\s*|\s*\(\s*|\s+)"
    r"(?=(?:"
    r"từ|đến|kể\s+từ|bổ\s+nhiệm|miễn\s+nhiệm|từ\s+nhiệm|"
    r"thôi\s+giữ|ngày"
    r")\b).*$",
    flags=re.IGNORECASE,
)
_ROLE_TRAILING_CLAUSE_RE = re.compile(
    r"\s+(?=(?:được|do|theo)\b).*$",
    flags=re.IGNORECASE,
)
_CORPORATE_EVENT_DATE_RE = re.compile(
    r"\b(?:"
    r"ngày\s+\d{1,2}\s+tháng\s+\d{1,2}\s+năm\s+(?:19|20)\d{2}|"
    r"\d{1,2}[./-]\d{1,2}[./-](?:19|20)\d{2}"
    r")\b",
    flags=re.IGNORECASE,
)
_CORPORATE_IDENTIFIER_RE = re.compile(
    r"\b(?P<label>"
    r"quyết\s+định(?:\s+của\s+[^,.;:]{1,80})?\s+số|"
    r"giấy\s+chứng\s+nhận\s+(?:đăng\s+ký\s+"
    r"(?:doanh\s+nghiệp|kinh\s+doanh)|đầu\s+tư)\s+số|"
    r"giấy\s+phép(?:\s+(?:đầu\s+tư|thành\s+lập|hoạt\s+động))?\s+số|"
    r"mã\s+số\s+(?:doanh\s+nghiệp|đăng\s+ký\s+kinh\s+doanh)"
    r")\s*[:#-]?\s*"
    r"(?P<identifier>"
    r"(?=[A-ZÀ-ỸĐ0-9./_-]*\d)"
    r"[A-ZÀ-ỸĐ0-9][A-ZÀ-ỸĐ0-9./_-]{1,79}"
    r")",
    flags=re.IGNORECASE,
)
_CORPORATE_EVENT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "Đăng ký doanh nghiệp",
        re.compile(
            r"\b(?:giấy\s+chứng\s+nhận\s+)?đăng\s+ký\s+"
            r"(?:doanh\s+nghiệp|kinh\s+doanh)\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "Niêm yết",
        re.compile(
            r"\b(?:niêm\s+yết|đăng\s+ký\s+giao\s+dịch)\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "Thành lập doanh nghiệp",
        re.compile(
            r"\b(?:được\s+)?thành\s+lập\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "Cổ phần hóa",
        # Vietnamese sources use both modern "hóa" (accent on o) and
        # traditional "hoá" (accent on a); OCR may also drop the accent.
        re.compile(
            r"\bcổ\s+phần\s+(?:hóa|hoá|hoa)\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "Đổi tên doanh nghiệp",
        re.compile(
            r"\b(?:đổi|thay\s+đổi)\s+tên\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "Sáp nhập hoặc hợp nhất",
        re.compile(
            r"\b(?:sáp\s+nhập|hợp\s+nhất)\s+" r"(?:với|vào|doanh\s+nghiệp|công\s+ty)\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "Giải thể doanh nghiệp",
        re.compile(
            r"\bgiải\s+thể(?:\s+(?:doanh\s+nghiệp|công\s+ty|chi\s+nhánh))?\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "Chuyển nhượng hoặc chuyển giao",
        re.compile(
            r"\b(?:chuyển\s+nhượng|chuyển\s+giao)\s+"
            r"(?:vốn|cổ\s+phần|doanh\s+nghiệp|công\s+ty|chi\s+nhánh|"
            r"dự\s+án|quyền\s+sở\s+hữu)\b",
            flags=re.IGNORECASE,
        ),
    ),
)
_EMPLOYEE_METRIC_RE = re.compile(
    r"\b(?:số\s+lượng\s+)?(?:nhân\s+viên|người\s+lao\s+động|lao\s+động)\b"
    r".{0,160}\b\d[\d.,]*\s*(?:người|nhân\s+viên|lao\s+động)?\b",
    flags=re.IGNORECASE,
)

# The vocabulary is intentionally asset-class based rather than report based.
# More specific classes have lower ranks so a subsection such as "Nhãn hiệu"
# wins over its parent heading "Tài sản cố định vô hình".
_ASSET_CLASS_RULES: tuple[tuple[int, re.Pattern[str]], ...] = (
    (
        -1,
        re.compile(
            r"\b(?P<entity>quyền\s+sử\s+dụng\s+đất)"
            r"\s+(?P<modifier>lâu\s+dài|có\s+thời\s+hạn)\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        0,
        re.compile(
            r"\b(?P<entity>quyền\s+sử\s+dụng\s+đất)\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        0,
        re.compile(
            r"\b(?P<entity>(?:phần\s+mềm(?:\s+(?:máy\s+vi\s+tính|máy\s+tính))?"
            r"|chương\s+trình\s+phần\s+mềm))\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        0,
        re.compile(
            r"\b(?P<entity>nhãn\s+hiệu(?:\s+hàng\s+h[oó]a)?)\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        0,
        re.compile(
            r"\b(?P<entity>bản\s+quyền|bằng\s+sáng\s+chế|"
            r"quyền\s+phát\s+hành|giấy\s+phép)\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        1,
        re.compile(
            r"\b(?P<entity>"
            r"nhà\s+cửa(?:,\s*vật\s+kiến\s+trúc)?|"
            r"vật\s+kiến\s+trúc|"
            r"máy\s+móc(?:,\s*thiết\s+bị)?|"
            r"thiết\s+bị|"
            r"phương\s+tiện\s+vận\s+tải(?:,\s*truyền\s+dẫn)?|"
            r"thiết\s+bị\s+văn\s+phòng|"
            r"cây\s+lâu\s+năm|súc\s+vật\s+làm\s+việc"
            r")\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        2,
        re.compile(
            r"\b(?P<entity>"
            r"tài\s+sản\s+cố\s+định(?:\s+(?:hữu\s+hình|vô\s+hình))?|"
            r"bất\s+động\s+sản\s+đầu\s+tư|"
            r"công\s+cụ(?:,\s*dụng\s+cụ)?|"
            r"chi\s+phí\s+trả\s+trước"
            r")\b",
            flags=re.IGNORECASE,
        ),
    ),
)


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _asset_class_entity(
    sentence: str,
    *,
    section: str = "",
    subsection: str = "",
) -> str:
    """Return the most specific explicitly available asset class.

    Narrative is preferred when two candidates have the same specificity, but
    a specific subsection label is allowed to beat a generic class mentioned
    in the sentence.  This is what binds a heading-only policy sentence such as
    "Khấu hao ... trong 3 năm" to the subsection "Nhãn hiệu".
    """

    candidates: list[tuple[int, int, int, str]] = []
    for source_rank, text in enumerate((sentence, subsection, section)):
        cleaned_source = _clean_text(text)
        if not cleaned_source:
            continue
        for specificity, pattern in _ASSET_CLASS_RULES:
            for match in pattern.finditer(cleaned_source):
                entity = _clean_text(match.group("entity")).strip(" :-–—()")
                modifier = (
                    _clean_text(match.groupdict().get("modifier", ""))
                    if "modifier" in match.groupdict()
                    else ""
                )
                if modifier:
                    entity = f"{entity} {modifier}"
                if entity:
                    candidates.append(
                        (specificity, source_rank, match.start(), entity)
                    )
    if not candidates:
        return ""
    return min(candidates)[3]


def _self_contained_policy_value(sentence: str, entity: str) -> str:
    """Prefix inherited heading context when it is absent from the atom."""

    cleaned_sentence = _clean_text(sentence)
    cleaned_entity = _clean_text(entity)
    if not cleaned_entity:
        return cleaned_sentence
    if cleaned_entity.casefold() in cleaned_sentence.casefold():
        return cleaned_sentence
    return f"{cleaned_entity}. {cleaned_sentence}"


def _event_topic(text: str) -> str:
    if _APPOINTMENT_RE.search(text):
        return "Bổ nhiệm nhân sự"
    if _DEPARTURE_RE.search(text):
        return "Miễn nhiệm hoặc từ nhiệm nhân sự"
    return "Thay đổi nhân sự"


def _person_clause(sentence: str, start: int, end: int) -> str:
    """Return the semicolon-delimited clause containing a person mention."""

    left = sentence.rfind(";", 0, start)
    right = sentence.find(";", end)
    return sentence[left + 1 : right if right >= 0 else len(sentence)].strip()


def _date_for_person(
    sentence: str,
    *,
    person_start: int,
    person_end: int,
) -> str:
    """Bind an explicit event date conservatively to one named person."""

    clause = _person_clause(sentence, person_start, person_end)
    clause_dates = list(_CORPORATE_EVENT_DATE_RE.finditer(clause))
    if len(clause_dates) == 1:
        return _clean_text(clause_dates[0].group(0))

    dates = list(_CORPORATE_EVENT_DATE_RE.finditer(sentence))
    if len(dates) == 1:
        return _clean_text(dates[0].group(0))
    if not dates:
        return ""

    preceding = [match for match in dates if match.end() <= person_start]
    if preceding:
        return _clean_text(preceding[-1].group(0))
    following = [match for match in dates if match.start() >= person_end]
    if following:
        return _clean_text(following[0].group(0))
    return ""


def _corporate_identifier_metric(label: str) -> str:
    cleaned = _clean_text(label).strip(" :-–—")
    if cleaned.casefold().startswith("mã số "):
        return cleaned[:1].upper() + cleaned[1:]
    without_number = re.sub(
        r"\s+số\s*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return f"Số {without_number.casefold()}"


def _sentences(paragraph: str) -> list[str]:
    text = _clean_text(paragraph)
    if not text:
        return []
    return [
        sentence.strip(" \t\r\n")
        for sentence in _SENTENCE_BOUNDARY_RE.split(text)
        if sentence.strip(" \t\r\n")
    ]


def _person_role_atoms(sentence: str) -> list[tuple[str, str, str]]:
    """Extract explicit person/role pairs without inferring a role from context."""

    matches = list(_PERSON_ROLE_NAME_RE.finditer(sentence))
    extracted: list[tuple[str, str, str]] = []
    for index, match in enumerate(matches):
        segment_end = (
            matches[index + 1].start() if index + 1 < len(matches) else len(sentence)
        )
        tail = sentence[match.end() : segment_end]
        role_match = _DELIMITED_ROLE_RE.match(tail)
        prose_match = False
        if not role_match:
            role_match = _PROSE_ROLE_RE.match(tail)
            prose_match = bool(role_match)
        if not role_match:
            continue

        raw_role = _clean_text(role_match.group("role")).strip(" \t\r\n-*•|;,.()")
        if not raw_role:
            continue

        # A pipe after the role is a table-like qualifier/date cell.  Keep it
        # in the source phrase, but never let it pollute the exact role label.
        metric_role = raw_role.split("|", 1)[0].strip()
        metric_role = _ROLE_QUALIFIER_RE.sub("", metric_role).strip(" \t\r\n-*•|;,.")
        metric_role = _ROLE_TRAILING_CLAUSE_RE.sub("", metric_role).strip(
            " \t\r\n-*•|;,."
        )
        if (
            not metric_role
            or len(metric_role) > 180
            or not _PERSON_ROLE_MARKER_RE.match(metric_role)
        ):
            continue

        source_phrase = sentence[match.start() : segment_end].strip(" \t\r\n-*•|;")
        # In "Ngày ..., HĐQT bổ nhiệm Bà X làm ...", the event date precedes
        # the person.  Preserve the complete source sentence for provenance.
        if prose_match and (
            _PERSON_EVENT_RE.search(sentence[: match.start()])
            or _CORPORATE_EVENT_DATE_RE.search(sentence[: match.start()])
        ):
            source_phrase = sentence
        extracted.append(
            (
                match.group("name").strip(),
                metric_role,
                _clean_text(source_phrase),
            )
        )
    return extracted


def derive_topic_atoms(
    paragraph: str,
    *,
    section: str = "",
    subsection: str = "",
) -> list[TopicAtom]:
    """Return conservative, deduplicated policy/listing atoms.

    The caller keeps the original paragraph as its own fact.  These atoms only
    add a focused retrieval label; they never replace or rewrite source prose.
    """

    atoms: list[TopicAtom] = []
    seen: set[tuple[str, str, str]] = set()

    def add(
        topic: str,
        value: str,
        *,
        atom_type: str = "topic",
        metric_label: str = "",
        entity_label: str = "",
    ) -> None:
        cleaned = _clean_text(value)
        if not cleaned:
            return
        cleaned_entity = _clean_text(entity_label)
        key = (
            topic.casefold(),
            cleaned.casefold(),
            cleaned_entity.casefold(),
        )
        if key in seen:
            return
        seen.add(key)
        atoms.append(
            TopicAtom(
                topic=topic,
                value=cleaned,
                atom_type=atom_type,
                metric_label=_clean_text(metric_label) or topic,
                entity_label=cleaned_entity,
            )
        )

    for sentence in _sentences(paragraph):
        policy_entity = _asset_class_entity(
            sentence,
            section=section,
            subsection=subsection,
        )

        def add_policy(topic: str) -> None:
            add(
                topic,
                _self_contained_policy_value(sentence, policy_entity),
                atom_type="policy",
                entity_label=policy_entity,
            )

        duration_match = _DURATION_RE.search(sentence)
        if duration_match:
            lowered = sentence.casefold()
            if "khấu hao" in lowered or "hao mòn" in lowered:
                add_policy("Thời gian khấu hao")
            if "phân bổ" in lowered:
                add_policy("Thời gian phân bổ")

        if _CONDITION_RE.search(sentence):
            if _RECOGNITION_RE.search(sentence):
                add_policy("Điều kiện ghi nhận")
            if _CAPITALIZATION_RE.search(sentence):
                add_policy("Điều kiện vốn hóa")

        if _DEPRECIATION_METHOD_RE.search(sentence):
            add_policy("Phương pháp khấu hao")
        if _AMORTIZATION_METHOD_RE.search(sentence):
            add_policy("Phương pháp phân bổ")
        if _NON_DEPRECIATION_RE.search(sentence):
            add_policy("Chính sách không khấu hao")
        if _MEASUREMENT_BASIS_RE.search(sentence):
            add_policy("Cơ sở đo lường")

        if _LISTING_RE.search(sentence):
            add("Niêm yết và sàn giao dịch", sentence)

        ticker_match = _TICKER_RE.search(sentence)
        if ticker_match:
            ticker = ticker_match.group("ticker")
            if ticker == ticker.upper():
                add("Mã chứng khoán", ticker)

        if _EMPLOYEE_METRIC_RE.search(sentence):
            add(
                "Số lượng nhân viên",
                sentence,
                atom_type="person_metric",
            )

        for person, role, source_phrase in _person_role_atoms(sentence):
            add(
                "Vai trò nhân sự",
                source_phrase,
                atom_type="person_role",
                metric_label=role,
                entity_label=person,
            )

        if _PERSON_EVENT_RE.search(sentence):
            # An event atom is useful only when it can be bound to a person.
            # This deliberately rejects generic labour-policy prose such as
            # "chấm dứt hợp đồng lao động theo quy định".
            for person_match in _PERSON_NAME_RE.finditer(sentence):
                person = person_match.group("name").strip()
                clause = _person_clause(
                    sentence,
                    person_match.start(),
                    person_match.end(),
                )
                event_text = clause if _PERSON_EVENT_RE.search(clause) else sentence
                if not _PERSON_EVENT_RE.search(event_text):
                    continue
                topic = _event_topic(event_text)
                add(
                    topic,
                    sentence,
                    atom_type="person_event",
                    entity_label=person,
                )
                event_date = _date_for_person(
                    sentence,
                    person_start=person_match.start(),
                    person_end=person_match.end(),
                )
                if event_date:
                    date_topic = f"Ngày {topic.casefold()}"
                    add(
                        date_topic,
                        event_date,
                        atom_type="person_event_date",
                        metric_label=date_topic,
                        entity_label=person,
                    )

        if _CORPORATE_EVENT_DATE_RE.search(sentence):
            for topic, marker in _CORPORATE_EVENT_RULES:
                if marker.search(sentence):
                    add(
                        topic,
                        sentence,
                        atom_type="corporate_event",
                        metric_label=topic,
                    )
                    date_topic = f"Ngày {topic.casefold()}"
                    for date_match in _CORPORATE_EVENT_DATE_RE.finditer(sentence):
                        add(
                            date_topic,
                            date_match.group(0),
                            atom_type="corporate_event_date",
                            metric_label=date_topic,
                        )
                    for identifier_match in _CORPORATE_IDENTIFIER_RE.finditer(
                        sentence
                    ):
                        identifier_topic = _corporate_identifier_metric(
                            identifier_match.group("label")
                        )
                        add(
                            identifier_topic,
                            identifier_match.group("identifier").rstrip(".,;:"),
                            atom_type="corporate_event_identifier",
                            metric_label=identifier_topic,
                        )

    return atoms
