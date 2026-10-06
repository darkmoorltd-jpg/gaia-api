# app/routers/purchases.py
import os
import httpx
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from app.services.auth import verify_supabase_token
from supabase import create_client

router = APIRouter()

PAYSTACK_SECRET = os.environ.get('PAYSTACK_SECRET_KEY', '')
SUPABASE_URL = os.environ.get('SUPABASE_URL', 'https://pxvtvuwlpzwlkdoxjrep.supabase.co')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')

PLANS = {
    'starter':    {'name': 'Starter',    'scans': 150,  'amount_kobo': 300000},
    'pro':        {'name': 'Pro',        'scans': 300,  'amount_kobo': 500000},
    'business':   {'name': 'Business',   'scans': 1000, 'amount_kobo': 1000000},
    'enterprise': {'name': 'Enterprise', 'scans': 5000, 'amount_kobo': 2000000},
}


class VerifyRequest(BaseModel):
    reference: str
    plan: str


def get_service():
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


@router.get('/plans')
async def list_plans():
    return {
        'plans': [
            {'key': k, 'name': v['name'], 'scans': v['scans'], 'amount_kobo': v['amount_kobo']}
            for k, v in PLANS.items()
        ]
    }


@router.post('/verify-payment')
async def verify_payment(req: VerifyRequest, authorization: str = Header(None)):
    if not PAYSTACK_SECRET:
        raise HTTPException(500, 'PAYSTACK_SECRET_KEY not configured')
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401, 'Missing Bearer token')
    token = authorization.split(' ', 1)[1]
    try:
        user = verify_supabase_token(token)
    except Exception as e:
        raise HTTPException(401, 'Invalid token: ' + str(e))

    user_id = user['sub']
    user_email = (user.get('email') or '').lower()
    plan = PLANS.get(req.plan)
    if not plan:
        raise HTTPException(400, 'Unknown plan: ' + req.plan)

    service = get_service()

    existing = service.table('payment_history').select('id').eq('reference', req.reference).execute()
    if existing.data and len(existing.data) > 0:
        s = service.table('user_scans').select('scans_remaining, plan').eq('user_id', user_id).execute()
        return {
            'ok': True,
            'already_credited': True,
            'scans_added': 0,
            'scans_remaining': s.data[0]['scans_remaining'] if s.data else 30,
            'plan': s.data[0]['plan'] if s.data else 'free',
        }

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            'https://api.paystack.co/transaction/verify/' + req.reference,
            headers={'Authorization': 'Bearer ' + PAYSTACK_SECRET},
        )
    if r.status_code != 200:
        raise HTTPException(502, 'Paystack verify failed: ' + str(r.status_code))

    data = r.json()
    txn = data.get('data', {}) or {}
    if txn.get('status') != 'success':
        raise HTTPException(400, 'Payment not successful: ' + str(txn.get('status')))

    paid_kobo = int(txn.get('amount', 0))
    if paid_kobo < plan['amount_kobo']:
        raise HTTPException(400, 'Amount paid is less than plan price')

    txn_email = ((txn.get('customer') or {}).get('email') or '').lower()
    if txn_email and user_email and txn_email != user_email:
        raise HTTPException(400, 'Payment email does not match account')

    service.table('user_scans').upsert(
        {'user_id': user_id, 'scans_remaining': 30, 'plan': 'free'},
        on_conflict='user_id',
    ).execute()

    current = service.table('user_scans').select('scans_remaining').eq('user_id', user_id).execute()
    cur_scans = current.data[0]['scans_remaining'] if current.data else 30
    new_total = cur_scans + plan['scans']

    service.table('user_scans').update({
        'scans_remaining': new_total,
        'plan': req.plan,
    }).eq('user_id', user_id).execute()

    service.table('payment_history').insert({
        'user_id': user_id,
        'amount': paid_kobo / 100.0,
        'scans_added': plan['scans'],
        'plan': req.plan,
        'reference': req.reference,
    }).execute()

    return {
        'ok': True,
        'already_credited': False,
        'scans_added': plan['scans'],
        'scans_remaining': new_total,
        'plan': req.plan,
    }
