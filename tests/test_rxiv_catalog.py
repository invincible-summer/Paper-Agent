from __future__ import annotations
from pathlib import Path
from tools.search.rxiv_catalog import search_catalog, sync_status, update_sync_state, upsert_records


def test_rxiv_upsert_search_and_version_idempotency(tmp_path):
    db=tmp_path/"catalog.db"
    rows=[{"doi":"10.1101/2024.01.01.123456","version":"1","title":"Single cell atlas",
           "authors":"Alice A; Bob B","abstract":"A neural cell atlas study", "category":"bioinformatics",
           "date":"2024-01-02","license":"cc_by"}]
    assert upsert_records("biorxiv",rows,path=db)==1
    assert upsert_records("biorxiv",rows,path=db)==0
    papers=search_catalog("biorxiv","single cell atlas",10,path=db)
    assert len(papers)==1 and papers[0].doi==rows[0]["doi"]
    older=[{**rows[0],"version":"0","title":"Old title"}]
    assert upsert_records("biorxiv",older,path=db)==0
    assert search_catalog("biorxiv","single cell atlas",10,path=db)[0].title=="Single cell atlas"
    newer=[{**rows[0],"version":"2","title":"Updated single cell atlas"}]
    assert upsert_records("biorxiv",newer,path=db)==1
    assert search_catalog("biorxiv","updated single cell",10,path=db)[0].title.startswith("Updated")
    assert "biorxiv.org/content/10.1101/2024.01.01.123456v2" in search_catalog(
        "biorxiv", "updated single cell", 10, path=db
    )[0].urls["biorxiv"]


def test_rxiv_sync_status_is_local_and_resumable(tmp_path):
    db=tmp_path/"catalog.db"
    update_sync_state("medrxiv",cursor=200,path=db)
    status=sync_status("medrxiv",path=db)
    assert status["server"]=="medrxiv"
    assert status["backfill_date"]=="2019-06-01"
    assert status["backfill_cursor"]==200
    assert status["indexed_count"]==0
