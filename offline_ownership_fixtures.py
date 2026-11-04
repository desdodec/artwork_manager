"""Synthetic ownership responses for offline tests only."""
from publisher import DATASET, ROLES

def preflight(rows, **extra):
    owners=[]
    for index,row in enumerate(rows):
        for role,slot in ROLES.items():
            owners.append(dict(key=row[slot],role=role,dataset_id=DATASET,
                street_key=row['street_key'],design_id='design-'+str(index),
                asset_id='asset-'+str(index),product_sku='GLOBAL-MUG-W',version=1,status='hold'))
    return dict(safe_to_upload=True,ownership_snapshot_version=1,ownership_complete=True,png_ownership=owners,**extra)
