# Attribution and provenance

This project collects and organises Indian parliamentary questions and answers from official parliamentary sources. Its collection, processing and export architecture was developed for this repository, drawing on earlier local research scripts. The provenance of every earlier script cannot be conclusively established.

During research, [CMHLP/archive](https://github.com/CMHLP/archive), [SansadSaar](https://github.com/indiavotes/SansadSaar), and [ParliamentWatch](https://github.com/pranaykotas/parliamentwatch) were consulted for parliamentary data interfaces and collection methods. A review of the files examined did not identify substantial copied code from those projects; this is not an audit of every historical version.

A previous question-number verification safeguard resembled a date-handling pattern in [Commoner-probe](https://github.com/CommonerLLP/commoner-probe/blob/master/commoner_probe/qno_guard.py), by Commoner LLP. That source credits an earlier zero-hour implementation and says it was copied with permission. The original zero-hour licence and downstream rights could not be independently established. The similar safeguard has been replaced with a separately written calendar-date check; this acknowledgement records the historical connection and does not imply that the overall project is based on Commoner-probe. The Commoner-probe MIT notice is included in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) as a precaution.

The project is independent of the Parliament of India and is not officially endorsed. The software licence does not grant rights in parliamentary records or documents.
