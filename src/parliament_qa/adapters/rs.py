"""Rajya Sabha source mapping and safe targeted filter construction."""
from datetime import datetime

API = "https://rsdoc.nic.in/Question/Search_Questions"
INTEGRATION = "https://integration.rajyasabha.digital/rsdebate/api/question/search-questions"
FIELDS = ("qtitle", "qn_text", "ans_text")


def clause(term):
    # Escape SQL Server LIKE metacharacters and quote literals; no raw syntax from YAML.
    literal = term.replace("'", "''").replace("[", "[[]").replace("%", "[%]").replace("_", "[_]")
    literal = "%".join(literal.split())
    return "(" + " OR ".join(f"{field} LIKE '%{literal}%'" for field in FIELDS) + ")"


def question(row):
    ident = str(row.get("qslno") or "")
    if not ident:
        raise ValueError("RS result lacks qslno")
    num = row.get("qno", "")
    if isinstance(num, float) and num.is_integer():
        num = int(num)
    day = str(row.get("adate") or "")[:10]
    if not day:
        try:
            day = datetime.strptime(str(row.get("ans_date", "")), "%d.%m.%Y").date().isoformat()
        except ValueError:
            day = ""
    return {"key": "RS:" + ident, "house": "RS", "source_id": ident, "term": "",
            "session": str(row.get("ses_no") or ""), "date": day, "question_number": str(num),
            "question_type": str(row.get("qtype") or "").strip(), "title": str(row.get("qtitle") or ""),
            "members": str(row.get("name") or ""), "ministry": str(row.get("min_name") or ""),
            "question_text": str(row.get("qn_text") or ""), "answer_text": str(row.get("ans_text") or ""),
            "text_source": "official rsdoc API", "official_url": API + "?whereclause=qslno%3D" + ident,
            "item_api_url": "", "source": "rsdoc"}
