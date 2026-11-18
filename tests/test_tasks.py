from app.tasks import collect_and_store_traffic, _last_raw
from app.models import TrafficSnapshot


def test_traffic_collector_accumulation_and_reset(db_session, sample_user):
    key = sample_user.vpn_keys[0]
    key.data_used = 1000
    db_session.commit()

    _last_raw.clear()

    # 1. First poll establishes baseline, delta is 0
    updated = collect_and_store_traffic(db_session, {key.uuid: 2000})
    assert updated == 0
    db_session.refresh(key)
    assert key.data_used == 1000

    # 2. Second poll: raw increased from 2000 to 2500 -> delta is 500
    updated = collect_and_store_traffic(db_session, {key.uuid: 2500})
    assert updated == 1
    db_session.refresh(key)
    assert key.data_used == 1500

    # Snapshot created
    snaps = db_session.query(TrafficSnapshot).filter(TrafficSnapshot.vpn_key_id == key.id).all()
    assert len(snaps) == 1
    assert snaps[0].bytes_delta == 500
    assert snaps[0].total_bytes == 1500

    # 3. Third poll: raw resets to 100 (container restart) -> delta is 100
    updated = collect_and_store_traffic(db_session, {key.uuid: 100})
    assert updated == 1
    db_session.refresh(key)
    assert key.data_used == 1600
