import tempfile
import zipfile
from pathlib import Path

import requests
from celery import shared_task

from market import models

MARKET_CONFIG = {
    "kosdaq": {"url_name": "kosdaq_code", "offset": -222},
    "kospi": {"url_name": "kospi_code", "offset": -228},
}


def download_and_parse_market(base_dir, market):
    config = MARKET_CONFIG[market.lower()]
    base_path = Path(base_dir)
    zip_path = base_path / f"{market}.zip"
    url = f"https://new.real.download.dws.co.kr/common/master/{config['url_name']}.mst.zip"

    # Download
    response = requests.get(url)
    response.raise_for_status()

    with open(zip_path, "wb") as f:
        f.write(response.content)

    # Extract
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(base_path)

    # Parse to List of Dicts
    mst_file = base_path / f"{config['url_name']}.mst"
    market_data = []

    with open(mst_file, mode="r", encoding="cp949") as f:
        for row in f:
            market_data.append(
                {
                    "market": market.upper(),
                    "ticker": row[0:9].strip(),
                    # "full_code": row[9:21].strip(),
                    "name": row[21 : config["offset"]].strip(),
                }
            )

    return market_data


@shared_task
def update_market():
    all_stock_data = []
    with tempfile.TemporaryDirectory() as tmp_dir:
        for market in MARKET_CONFIG:
            all_stock_data.extend(download_and_parse_market(tmp_dir, market))

    # Convert to model instances
    stock_instances = [models.Stock(**data) for data in all_stock_data]

    # UPSERT based on the composite unique key
    models.Stock.objects.bulk_create(
        stock_instances,
        update_conflicts=True,
        unique_fields=["ticker", "market"],
        update_fields=["name"],  # Fields to update on conflict
    )

    return {
        "stocks": len(all_stock_data),
    }
