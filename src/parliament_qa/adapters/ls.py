"""Lok Sabha DSpace discovery mapping, adapted from search_ls_multi_terms.py."""
API = "https://elibrary.sansad.in/server/api/discover/search/objects"
SCOPE = "75228d43-3a98-4b7d-b90f-ffec6aa9fa11"


def metadata(item, key):
    return " | ".join(str(x.get("value", "")) for x in item.get("metadata", {}).get(key, []) if x.get("value"))


def page(data):
    result = data.get("_embedded", {}).get("searchResult")
    if not isinstance(result, dict) or not isinstance(result.get("_embedded", {}).get("objects"), list):
        raise ValueError("DSpace response lacks searchResult objects")
    info = result.get("page")
    if not isinstance(info, dict) or "totalPages" not in info:
        raise ValueError("DSpace response lacks pagination")
    return result["_embedded"]["objects"], int(info["totalPages"])


def question(obj):
    item = obj.get("_embedded", {}).get("indexableObject", {})
    ident = str(item.get("uuid") or item.get("id") or "")
    if not ident:
        raise ValueError("DSpace result lacks item UUID")
    md = lambda key: metadata(item, key)
    handle = item.get("handle", "")
    return {"key": "LS:" + ident, "house": "LS", "source_id": ident,
            "term": md("dc.identifier.loksabhanumber"), "session": md("dc.identifier.sessionnumber"),
            "date": md("dc.date.issued")[:10], "question_number": md("dc.identifier.questionnumber"),
            "question_type": md("dc.identifier.questiontype"), "title": md("dc.title") or item.get("name", ""),
            "members": md("dc.contributor.members"), "ministry": md("dc.relation.ministry"),
            "question_text": "", "answer_text": "", "text_source": "unavailable",
            "official_url": md("dc.identifier.uri") or ("https://elibrary.sansad.in/handle/" + handle if handle else ""),
            "item_api_url": obj.get("_links", {}).get("indexableObject", {}).get("href", ""),
            "source": "DSpace"}
