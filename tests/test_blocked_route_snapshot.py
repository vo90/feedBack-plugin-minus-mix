"""Older manager snapshots must retain the row explaining a service block."""
import routes


def test_public_batch_retains_blocked_row_before_trailing_queue():
    items = [
        {"source_filename": f"song-{index}.feedpak", "status": "queued"}
        for index in range(routes.MAX_PUBLIC_BATCH_ITEMS + 25)
    ]
    items[1].update(status="blocked", detail="The selected model is not installed")
    result = routes._public_batch({"status": "blocked", "items": items})
    assert result["items_truncated"] is True
    assert result["items_total"] == len(items)
    assert len(result["items"]) == routes.MAX_PUBLIC_BATCH_ITEMS
    assert items[1] in result["items"]
