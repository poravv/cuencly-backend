#!/usr/bin/env python3
"""
Backfill de owner_email para datos existentes y migración de configs a multiusuario.

Opciones de asignación (correr selectivamente, con --dry-run para verificar):

- Por RUC del receptor (empresa del usuario): --map-receptor-ruc mapping.json
  mapping.json -> { "80012345-6": "andres@dominio.com", "1234567-8": "carlos@dominio.com" }

- Por email del receptor: --map-receptor-email mapping.json
  mapping.json -> { "andres@dominio.com": "andres@dominio.com", "carlos@dominio.com": "carlos@dominio.com" }

- Por defecto (todos los restantes): --default-owner someone@dominio.com

- Migrar email_configs (IMAP) a multiusuario: --map-config-username mapping.json
  mapping.json -> { "imap-user-1@dominio.com": "andres@dominio.com", "imap-user-2@dominio.com": "carlos@dominio.com" }

Ejemplos:
  python backend/scripts/backfill_owner_email.py --map-receptor-ruc ruc_map.json --map-config-username cfg_map.json --dry-run
  python backend/scripts/backfill_owner_email.py --map-receptor-email email_map.json --default-owner admin@dominio.com

Requiere variables de entorno MONGODB_URL y MONGODB_DATABASE (o por settings).
"""

import argparse
import json
import os
from typing import Dict, Any, List, Optional

from pymongo import MongoClient


def load_json(path: Optional[str]) -> Dict[str, str]:
    if not path:
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def get_db() -> Any:
    mongo_url = os.getenv('MONGODB_URL') or os.getenv('MONGODB_CONNECTION_STRING')
    if not mongo_url:
        from app.config.settings import settings  # type: ignore
        mongo_url = settings.MONGODB_URL
        db_name = settings.MONGODB_DATABASE
    else:
        db_name = os.getenv('MONGODB_DATABASE') or 'cuenlyapp_warehouse'
    client = MongoClient(mongo_url)
    client.admin.command('ping')
    return client[db_name]


def assign_headers_by_receptor_ruc(db, ruc_map: Dict[str, str], dry_run: bool) -> int:
    if not ruc_map:
        return 0
    coll = db['invoice_headers']
    total_updated = 0
    for ruc, owner in ruc_map.items():
        q = { 'receptor.ruc': ruc, '$or': [ {'owner_email': {'$exists': False}}, {'owner_email': ''} ] }
        if dry_run:
            cnt = coll.count_documents(q)
        else:
            res = coll.update_many(q, {'$set': {'owner_email': owner.lower()}})
            cnt = res.modified_count
        total_updated += cnt
        print(f"[headers] receptor.ruc={ruc} -> owner={owner} | {'to_update' if dry_run else 'updated'}={cnt}")
    return total_updated


def assign_headers_by_receptor_email(db, email_map: Dict[str, str], dry_run: bool) -> int:
    if not email_map:
        return 0
    coll = db['invoice_headers']
    total_updated = 0
    for rec_email, owner in email_map.items():
        q = { 'receptor.email': rec_email, '$or': [ {'owner_email': {'$exists': False}}, {'owner_email': ''} ] }
        if dry_run:
            cnt = coll.count_documents(q)
        else:
            res = coll.update_many(q, {'$set': {'owner_email': owner.lower()}})
            cnt = res.modified_count
        total_updated += cnt
        print(f"[headers] receptor.email={rec_email} -> owner={owner} | {'to_update' if dry_run else 'updated'}={cnt}")
    return total_updated


def assign_headers_default(db, default_owner: Optional[str], dry_run: bool) -> int:
    if not default_owner:
        return 0
    coll = db['invoice_headers']
    q = { '$or': [ {'owner_email': {'$exists': False}}, {'owner_email': ''} ] }
    if dry_run:
        cnt = coll.count_documents(q)
    else:
        res = coll.update_many(q, {'$set': {'owner_email': default_owner.lower()}})
        cnt = res.modified_count
    print(f"[headers] default owner={default_owner} | {'to_update' if dry_run else 'updated'}={cnt}")
    return cnt


def backfill_items_from_headers(db, dry_run: bool) -> int:
    headers = db['invoice_headers']
    items = db['invoice_items']
    # Tomar headers con owner_email definido
    cursor = headers.find({ 'owner_email': { '$exists': True, '$ne': '' } }, { '_id': 1, 'owner_email': 1 })
    total = 0
    for h in cursor:
        hid = h.get('_id')
        ow = h.get('owner_email', '').lower()
        if not hid or not ow:
            continue
        q = { 'header_id': hid, '$or': [ {'owner_email': {'$exists': False}}, {'owner_email': ''} ] }
        if dry_run:
            cnt = items.count_documents(q)
        else:
            res = items.update_many(q, {'$set': {'owner_email': ow}})
            cnt = res.modified_count
        total += cnt
    print(f"[items] {'to_update' if dry_run else 'updated'} total={total}")
    return total


def migrate_email_configs(db, username_map: Dict[str, str], dry_run: bool) -> int:
    if not username_map:
        return 0
    coll = db['email_configs']
    total = 0
    for username, owner in username_map.items():
        q = { 'username': username }
        upd = { '$set': { 'owner_email': owner.lower() } }
        if dry_run:
            cnt = coll.count_documents(q)
        else:
            res = coll.update_many(q, upd)
            cnt = res.modified_count
        total += cnt
        print(f"[email_configs] username={username} -> owner={owner} | {'to_update' if dry_run else 'updated'}={cnt}")
    return total


def main():
    parser = argparse.ArgumentParser(description='Backfill owner_email y migración a multiusuario')
    parser.add_argument('--map-receptor-ruc', help='JSON mapping receptor RUC -> owner_email')
    parser.add_argument('--map-receptor-email', help='JSON mapping receptor email -> owner_email')
    parser.add_argument('--map-config-username', help='JSON mapping email_configs.username -> owner_email')
    parser.add_argument('--default-owner', help='Owner por defecto para cabeceras sin asignar')
    parser.add_argument('--dry-run', action='store_true', help='No escribir cambios, solo contar')
    args = parser.parse_args()

    db = get_db()
    ruc_map = load_json(args.map_receptor_ruc)
    rec_email_map = load_json(args.map_receptor_email)
    if not args.default_owner:
        args.default_owner = os.getenv('DEFAULT_OWNER_EMAIL')
    cfg_map = load_json(args.map_config_username)

    print('--- Backfill owner_email (headers) ---')
    n1 = assign_headers_by_receptor_ruc(db, ruc_map, args.dry_run)
    n2 = assign_headers_by_receptor_email(db, rec_email_map, args.dry_run)
    n3 = assign_headers_default(db, args.default_owner, args.dry_run)
    print(f'Total headers affected: {n1 + n2 + n3}')

    print('--- Backfill owner_email (items) ---')
    n_items = backfill_items_from_headers(db, args.dry_run)
    print(f'Total items affected: {n_items}')

    print('--- Migrar email_configs a multiusuario ---')
    n_cfg = migrate_email_configs(db, cfg_map, args.dry_run)
    print(f'Configs affected: {n_cfg}')

    print('DONE.')


if __name__ == '__main__':
    main()



