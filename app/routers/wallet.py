# app/routers/wallet.py
import os
import uuid
import hmac
import hashlib
import json
from datetime import datetime
import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
from app.services.auth import verify_supabase_token
from supabase import create_client

router = APIRouter()

PAYSTACK_SECRET = os.environ.get('PAYSTACK_SECRET_KEY', '')
PAYSTACK_API = 'https://api.paystack.co'
SUPABASE_URL = os.environ.get('SUPABASE_URL', 'https://pxvtvuwlpzwlkdoxjrep.supabase.co')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')
DVA_BANK = os.environ.get('PAYSTACK_DVA_BANK', 'wema-bank')


def svc():
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def auth_user(authorization):
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401, 'Missing Bearer token')
    token = authorization.split(' ', 1)[1]
    try:
        return verify_supabase_token(token)
    except Exception as e:
        raise HTTPException(401, 'Invalid token: ' + str(e))


def _require_kyc(uid: str):
    """Raise 403 if user is not verified (KYC approved)."""
    s = svc()
    try:
        r = s.rpc('user_is_verified', {'p_user_id': uid}).execute()
        if not (r.data is True or r.data == True):
            raise HTTPException(403, 'Verify your identity in Profile first')
    except HTTPException:
        raise
    except Exception:
        # If RPC missing, fail open (admin only path)
        pass


async def ps_post(path, body):
    async with httpx.AsyncClient(timeout=30) as c:
        return await c.post(
            PAYSTACK_API + path,
            headers={'Authorization': 'Bearer ' + PAYSTACK_SECRET, 'Content-Type': 'application/json'},
            json=body,
        )


async def ps_get(path):
    async with httpx.AsyncClient(timeout=30) as c:
        return await c.get(PAYSTACK_API + path, headers={'Authorization': 'Bearer ' + PAYSTACK_SECRET})


class ProvisionReq(BaseModel):
    bvn: str
    first_name: str
    last_name: str
    phone: str


@router.post('/wallet/provision')
async def wallet_provision(req: ProvisionReq, authorization: str = Header(None)):
    user = auth_user(authorization)
    uid = user['sub']
    email = user.get('email') or ''
    s = svc()
    ww = s.table('farmer_wallets').select('*').eq('user_id', uid).limit(1).execute()
    if ww.data and len(ww.data) > 0 and ww.data[0].get('account_number'):
        row = ww.data[0]
        return {'ok': True, 'already_provisioned': True, 'account_number': row['account_number'], 'bank_name': row.get('bank_name'), 'account_name': row.get('account_name')}
    cust = await ps_post('/customer', {'email': email, 'first_name': req.first_name, 'last_name': req.last_name, 'phone': req.phone})
    cd = cust.json()
    if not cd.get('status'):
        raise HTTPException(400, cd.get('message') or 'Customer creation failed')
    customer_code = cd['data']['customer_code']
    acct_name = ('GAIA Money - ' + req.first_name + ' ' + req.last_name)[:100]
    dva = await ps_post('/dedicated_account', {'customer': customer_code, 'preferred_bank': DVA_BANK, 'first_name': req.first_name, 'last_name': req.last_name, 'phone': req.phone, 'bvn': req.bvn, 'account_name': acct_name})
    dd = dva.json()
    if not dd.get('status'):
        raise HTTPException(400, dd.get('message') or 'DVA creation failed')
    account_number = dd['data']['account_number']
    bank_name = (dd['data'].get('bank') or {}).get('name') or 'Wema Bank'
    s.table('farmer_wallets').upsert({
        'user_id': uid,
        'account_number': account_number,
        'account_name': acct_name,
        'bank_name': bank_name,
        'provider_customer_code': customer_code,
        'provider_dva_id': str(dd['data'].get('id') or ''),
        'provisioned_at': datetime.utcnow().isoformat(),
        'updated_at': datetime.utcnow().isoformat(),
    }, on_conflict='user_id').execute()
    return {'ok': True, 'account_number': account_number, 'bank_name': bank_name, 'account_name': acct_name}


@router.get('/wallet/me')
async def wallet_me(authorization: str = Header(None)):
    user = auth_user(authorization)
    uid = user['sub']
    s = svc()
    wallet_row = {}
    try:
        ww = s.table('farmer_wallets').select('*').eq('user_id', uid).limit(1).execute()
        if ww.data and len(ww.data) > 0:
            wallet_row = ww.data[0]
    except Exception as e:
        print('wallet fetch error: ' + str(e)[:200], flush=True)
    txns_data = []
    try:
        txns = s.table('wallet_transactions').select('*').eq('user_id', uid).order('created_at', desc=True).limit(30).execute()
        if txns.data:
            txns_data = txns.data
    except Exception as e:
        print('txns fetch error: ' + str(e)[:200], flush=True)
    return {'wallet': wallet_row, 'transactions': txns_data}


class DepositInitReq(BaseModel):
    amount_naira: float


@router.post('/wallet/deposit/init')
async def wallet_deposit_init(req: DepositInitReq, authorization: str = Header(None)):
    user = auth_user(authorization)
    uid = user['sub']
    if req.amount_naira < 100:
        raise HTTPException(400, 'Minimum deposit is N100')
    ref = 'GAIA_TOPUP_' + uid[:8] + '_' + uuid.uuid4().hex[:10]
    s = svc()
    s.table('wallet_transactions').insert({'user_id': uid, 'type': 'deposit', 'direction': 'in', 'amount': req.amount_naira, 'status': 'pending', 'reference': ref}).execute()
    return {'reference': ref, 'amount_kobo': int(round(req.amount_naira * 100)), 'email': user.get('email') or ''}


class DepositVerifyReq(BaseModel):
    reference: str


@router.post('/wallet/deposit/verify')
async def wallet_deposit_verify(req: DepositVerifyReq, authorization: str = Header(None)):
    user = auth_user(authorization)
    uid = user['sub']
    r = await ps_get('/transaction/verify/' + req.reference)
    if r.status_code != 200:
        raise HTTPException(502, 'Verify failed')
    d = r.json()
    txn = d.get('data') or {}
    if txn.get('status') != 'success':
        raise HTTPException(400, 'Payment not successful')
    paid_naira = float(txn.get('amount', 0)) / 100.0
    s = svc()
    existing = s.table('wallet_transactions').select('id,status').eq('reference', req.reference).limit(1).execute()
    if existing.data and len(existing.data) > 0 and existing.data[0].get('status') == 'success':
        ww = s.table('farmer_wallets').select('balance').eq('user_id', uid).limit(1).execute()
        return {'ok': True, 'already_credited': True, 'balance': float((ww.data[0] if ww.data else {}).get('balance') or 0)}
    # Ensure row exists, then atomic credit
    s.table('farmer_wallets').upsert({'user_id': uid, 'balance': 0}, on_conflict='user_id').execute()
    credit_res = s.rpc('wallet_credit', {'p_user_id': uid, 'p_amount': paid_naira}).execute()
    new_bal = credit_res.data
    s.table('wallet_transactions').update({'status': 'success', 'balance_after': new_bal, 'provider_ref': str(txn.get('id') or ''), 'updated_at': datetime.utcnow().isoformat()}).eq('reference', req.reference).execute()
    return {'ok': True, 'amount': paid_naira, 'balance': new_bal}


class SendUserReq(BaseModel):
    identifier: str
    amount_naira: float
    note: Optional[str] = None
    pin: str = ""


@router.post('/wallet/send/user')
async def wallet_send_user(req: SendUserReq, authorization: str = Header(None)):
    user = auth_user(authorization)
    uid = user['sub']
    _require_kyc(uid)
    _check_pin(uid, req.pin)
    s = svc()
    if req.amount_naira < 10:
        raise HTTPException(400, 'Minimum N10')
    ident = req.identifier.strip().lower()
    rec = None
    if '@' in ident:
        rr = s.table('user_profiles').select('user_id,first_name,last_name').eq('email', ident).limit(1).execute()
        rec = rr.data[0] if rr.data else None
    else:
        rr = s.table('user_profiles').select('user_id,first_name,last_name').eq('phone', ident).limit(1).execute()
        rec = rr.data[0] if rr.data else None
    if not rec:
        raise HTTPException(404, 'No GAIA user found')
    rid = rec['user_id']
    if rid == uid:
        raise HTTPException(400, 'Cannot send to yourself')
    # Ensure recipient row exists so credit doesn't fail on missing row
    s.table('farmer_wallets').upsert({'user_id': rid, 'balance': 0}, on_conflict='user_id').execute()

    # Atomic debit (checks balance + daily limit in one statement)
    debit_res = s.rpc('wallet_debit', {
        'p_user_id': uid,
        'p_amount': req.amount_naira,
        'p_daily_limit': 50000,
    }).execute()
    new_sender = debit_res.data
    if new_sender is None:
        raise HTTPException(400, 'Insufficient balance or daily limit reached')

    # Atomic credit to recipient
    credit_res = s.rpc('wallet_credit', {
        'p_user_id': rid,
        'p_amount': req.amount_naira,
    }).execute()
    new_rec = credit_res.data if credit_res.data is not None else 0
    out_ref = 'GAIA_P2P_OUT_' + uuid.uuid4().hex[:12]
    in_ref = 'GAIA_P2P_IN_' + uuid.uuid4().hex[:12]
    rec_name = ((rec.get('first_name') or '') + ' ' + (rec.get('last_name') or '')).strip() or ident
    s.table('wallet_transactions').insert([
        {'user_id': uid, 'type': 'p2p_send', 'direction': 'out', 'amount': req.amount_naira, 'balance_after': new_sender, 'status': 'success', 'reference': out_ref, 'counterparty_id': rid, 'counterparty_name': rec_name, 'meta': {'note': req.note or ''}},
        {'user_id': rid, 'type': 'p2p_receive', 'direction': 'in', 'amount': req.amount_naira, 'balance_after': new_rec, 'status': 'success', 'reference': in_ref, 'counterparty_id': uid, 'counterparty_name': (user.get('email') or '').split('@')[0], 'meta': {'note': req.note or ''}},
    ]).execute()
    return {'ok': True, 'balance': new_sender, 'recipient': rec_name}


class ResolveReq(BaseModel):
    account_number: str
    bank_code: str


@router.post('/wallet/resolve')
async def wallet_resolve(req: ResolveReq, authorization: str = Header(None)):
    auth_user(authorization)
    r = await ps_get('/bank/resolve?account_number=' + req.account_number + '&bank_code=' + req.bank_code)
    if r.status_code != 200:
        raise HTTPException(400, 'Could not resolve account')
    d = r.json()
    if not d.get('status'):
        raise HTTPException(400, d.get('message') or 'Resolve failed')
    return {'account_name': d['data']['account_name'], 'account_number': d['data']['account_number']}


@router.get('/wallet/banks')
async def wallet_banks():
    r = await ps_get('/bank?country=nigeria&perPage=100')
    if r.status_code != 200:
        raise HTTPException(502, 'Bank list failed')
    d = r.json()
    banks = [{'name': b['name'], 'code': b['code']} for b in d.get('data', [])]
    return {'banks': banks}


class WithdrawReq(BaseModel):
    account_number: str
    bank_code: str
    account_name: str
    amount_naira: float
    pin: str = ""


@router.post('/wallet/withdraw')
async def wallet_withdraw(req: WithdrawReq, authorization: str = Header(None)):
    user = auth_user(authorization)
    uid = user['sub']
    _require_kyc(uid)
    _check_pin(uid, req.pin)
    s = svc()
    if req.amount_naira < 500:
        raise HTTPException(400, 'Minimum N500')
    # Atomic debit BEFORE calling Paystack. Refund if transfer fails.
    debit_res = s.rpc('wallet_debit', {
        'p_user_id': uid,
        'p_amount': req.amount_naira,
        'p_daily_limit': 50000,
    }).execute()
    new_bal = debit_res.data
    if new_bal is None:
        raise HTTPException(400, 'Insufficient balance or daily limit reached')

    rec = await ps_post('/transferrecipient', {'type': 'nuban', 'name': req.account_name, 'account_number': req.account_number, 'bank_code': req.bank_code, 'currency': 'NGN'})
    rd = rec.json()
    if not rd.get('status'):
        # Refund
        s.rpc('wallet_credit', {'p_user_id': uid, 'p_amount': req.amount_naira}).execute()
        raise HTTPException(400, rd.get('message') or 'Recipient failed')
    recipient_code = rd['data']['recipient_code']
    ref = 'GAIA_WD_' + uuid.uuid4().hex[:12]
    s.table('wallet_transactions').insert({'user_id': uid, 'type': 'withdrawal', 'direction': 'out', 'amount': req.amount_naira, 'balance_after': new_bal, 'status': 'processing', 'reference': ref, 'counterparty_acct': req.account_number, 'counterparty_name': req.account_name}).execute()
    xfer = await ps_post('/transfer', {'source': 'balance', 'amount': int(round(req.amount_naira * 100)), 'recipient': recipient_code, 'reason': 'GAIA wallet withdrawal', 'reference': ref})
    xd = xfer.json()
    if not xd.get('status'):
        # Refund via atomic credit
        s.rpc('wallet_credit', {'p_user_id': uid, 'p_amount': req.amount_naira}).execute()
        s.table('wallet_transactions').update({'status': 'failed', 'failure_reason': xd.get('message') or 'Transfer failed'}).eq('reference', ref).execute()
        raise HTTPException(400, xd.get('message') or 'Transfer failed')
    s.table('wallet_transactions').update({'provider_ref': xd['data'].get('transfer_code') or ''}).eq('reference', ref).execute()
    return {'ok': True, 'balance': new_bal, 'reference': ref, 'status': 'processing'}


@router.post('/wallet/webhook/paystack')
async def wallet_webhook_paystack(request: Request):
    raw = await request.body()
    sig = request.headers.get('x-paystack-signature', '')
    expected = hmac.new(PAYSTACK_SECRET.encode(), raw, hashlib.sha512).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(401, 'Invalid signature')
    event = json.loads(raw)
    et = event.get('event')
    data = event.get('data') or {}
    s = svc()
    if et == 'charge.success':
        ref = data.get('reference') or ''
        cust = (data.get('customer') or {}).get('customer_code') or ''
        if cust:
            ww = s.table('farmer_wallets').select('user_id,balance').eq('provider_customer_code', cust).limit(1).execute()
            if ww.data and len(ww.data) > 0:
                uid = ww.data[0]['user_id']
                amt = float(data.get('amount', 0)) / 100.0
                s.table('farmer_wallets').upsert({'user_id': uid, 'balance': 0}, on_conflict='user_id').execute()
                credit_res = s.rpc('wallet_credit', {'p_user_id': uid, 'p_amount': amt}).execute()
                new_bal = credit_res.data
                ex = s.table('wallet_transactions').select('id').eq('reference', ref).limit(1).execute()
                if not ex.data or len(ex.data) == 0:
                    s.table('wallet_transactions').insert({'user_id': uid, 'type': 'deposit', 'direction': 'in', 'amount': amt, 'balance_after': new_bal, 'status': 'success', 'reference': ref, 'provider_ref': str(data.get('id') or ''), 'meta': {'source': 'dva'}}).execute()
    if et == 'transfer.success':
        ref = data.get('reference') or ''
        s.table('wallet_transactions').update({'status': 'success', 'updated_at': datetime.utcnow().isoformat()}).eq('reference', ref).execute()
    if et == 'transfer.failed' or et == 'transfer.reversed':
        ref = data.get('reference') or ''
        tx = s.table('wallet_transactions').select('user_id,amount,status').eq('reference', ref).limit(1).execute()
        if tx.data and len(tx.data) > 0 and tx.data[0].get('status') == 'processing':
            uid = tx.data[0]['user_id']
            amt = float(tx.data[0]['amount'])
            ww = s.table('farmer_wallets').select('balance').eq('user_id', uid).limit(1).execute()
            new_bal = (float((ww.data[0] if ww.data else {}).get('balance') or 0)) + amt
            s.table('farmer_wallets').update({'balance': new_bal}).eq('user_id', uid).execute()
            s.table('wallet_transactions').update({'status': 'refunded', 'balance_after': new_bal, 'failure_reason': data.get('reason') or et}).eq('reference', ref).execute()
    return {'ok': True}


# ============================================================
# Admin: provision by user_id — looks up BVN/name/phone internally
# ============================================================
class ProvisionByUserReq(BaseModel):
    user_id: str


@router.post('/wallet/provision-by-user')
async def wallet_provision_by_user(req: ProvisionByUserReq, authorization: str = Header(None)):
    # Admin gate
    admin = auth_user(authorization)
    admin_email = (admin.get('email') or '').lower()
    if admin_email != 'darkmoorltd@gmail.com':
        raise HTTPException(403, 'admin only')

    uid = req.user_id
    s = svc()

    # Already provisioned?
    existing = s.table('farmer_wallets').select('account_number,account_name,bank_name').eq('user_id', uid).limit(1).execute()
    if existing.data and len(existing.data) > 0 and existing.data[0].get('account_number'):
        row = existing.data[0]
        return {'ok': True, 'already_provisioned': True, 'account_number': row['account_number'], 'account_name': row.get('account_name'), 'bank_name': row.get('bank_name')}

    # Fetch verification
    v = s.table('farmer_verifications').select('full_name,bvn,phone,status').eq('user_id', uid).order('created_at', desc=True).limit(1).execute()
    if not v.data or len(v.data) == 0:
        raise HTTPException(400, 'No verification record for this user')
    verif = v.data[0]
    bvn = (verif.get('bvn') or '').strip()
    if not bvn:
        raise HTTPException(400, 'BVN missing on verification record')

    # Fetch profile for names
    p = s.table('user_profiles').select('first_name,last_name,phone,email').eq('user_id', uid).limit(1).execute()
    prof = p.data[0] if p.data and len(p.data) > 0 else {}
    first_name = (prof.get('first_name') or '').strip() or 'GAIA'
    last_name = (prof.get('last_name') or '').strip() or 'Farmer'
    phone = (prof.get('phone') or verif.get('phone') or '').strip()
    email = (prof.get('email') or '').strip()

    if not email:
        # fall back to auth email
        try:
            au = s.auth.admin.get_user_by_id(uid)
            email = au.user.email if au and au.user else ''
        except Exception:
            pass
    if not email:
        raise HTTPException(400, 'Email missing')

    if not phone:
        phone = '+2340000000000'

    # Create Paystack customer
    cust = await ps_post('/customer', {
        'email': email,
        'first_name': first_name,
        'last_name': last_name,
        'phone': phone,
    })
    cd = cust.json()
    if not cd.get('status'):
        # customer already exists is fine — fetch it
        if 'already' in (cd.get('message') or '').lower():
            r2 = await ps_get('/customer?email=' + email)
            rd = r2.json()
            if rd.get('status') and rd.get('data'):
                customer_code = rd['data'][0]['customer_code']
            else:
                raise HTTPException(400, cd.get('message') or 'Customer creation failed')
        else:
            raise HTTPException(400, cd.get('message') or 'Customer creation failed')
    else:
        customer_code = cd['data']['customer_code']

    acct_name = ('GAIA Money - ' + first_name + ' ' + last_name)[:100]

    # Create DVA
    dva = await ps_post('/dedicated_account', {
        'customer': customer_code,
        'preferred_bank': DVA_BANK,
        'first_name': first_name,
        'last_name': last_name,
        'phone': phone,
        'bvn': bvn,
        'account_name': acct_name,
    })
    dd = dva.json()
    if not dd.get('status'):
        raise HTTPException(400, dd.get('message') or 'DVA creation failed')

    account_number = dd['data']['account_number']
    bank_name = (dd['data'].get('bank') or {}).get('name') or 'Paystack-Titan'

    # Upsert farmer_wallets row
    s.table('farmer_wallets').upsert({
        'user_id': uid,
        'account_number': account_number,
        'account_name': acct_name,
        'bank_name': bank_name,
        'provider_customer_code': customer_code,
        'provider_dva_id': str(dd['data'].get('id') or ''),
        'provisioned_at': datetime.utcnow().isoformat(),
        'updated_at': datetime.utcnow().isoformat(),
    }, on_conflict='user_id').execute()

    return {
        'ok': True,
        'account_number': account_number,
        'account_name': acct_name,
        'bank_name': bank_name,
    }



# ============================================================
# PIN helpers — delegate to wallet_pin.py (single source of truth)
# ============================================================
from app.routers.wallet_pin import check_pin as _check_pin


class BuyScansReq(BaseModel):
    plan: str
    pin: str


@router.post('/wallet/buy-scans')
async def wallet_buy_scans(req: BuyScansReq, authorization: str = Header(None)):
    user = auth_user(authorization)
    uid = user['sub']
    _check_pin(uid, req.pin)

    # Local plan table (server side source of truth)
    PLAN_MAP = {
        'starter':    {'name': 'Starter',    'scans': 150,  'amount_naira': 3000},
        'pro':        {'name': 'Pro',        'scans': 300,  'amount_naira': 5000},
        'business':   {'name': 'Business',   'scans': 1000, 'amount_naira': 10000},
        'enterprise': {'name': 'Enterprise', 'scans': 5000, 'amount_naira': 20000},
    }
    plan = PLAN_MAP.get(req.plan)
    if not plan:
        raise HTTPException(400, 'Unknown plan')
    amt = float(plan['amount_naira'])

    s = svc()
    debit_res = s.rpc('wallet_debit', {
        'p_user_id': uid,
        'p_amount': amt,
        'p_daily_limit': 1000000,
    }).execute()
    new_bal = debit_res.data
    if new_bal is None:
        raise HTTPException(400, 'Insufficient wallet balance')

    # Credit scans
    s.table('user_scans').upsert(
        {'user_id': uid, 'scans_remaining': 30, 'plan': 'free'},
        on_conflict='user_id'
    ).execute()
    sc = s.table('user_scans').select('scans_remaining').eq('user_id', uid).limit(1).execute()
    cur = int((sc.data[0] if sc.data else {}).get('scans_remaining') or 0)
    new_scans = cur + plan['scans']
    s.table('user_scans').update({'scans_remaining': new_scans, 'plan': req.plan}).eq('user_id', uid).execute()

    ref = 'GAIA_WSCAN_' + uuid.uuid4().hex[:12]
    s.table('wallet_transactions').insert({
        'user_id': uid,
        'type': 'scan_purchase',
        'direction': 'out',
        'amount': amt,
        'balance_after': new_bal,
        'status': 'success',
        'reference': ref,
        'counterparty_name': plan['name'] + ' (' + str(plan['scans']) + ' scans)',
        'meta': {'plan': req.plan, 'scans_added': plan['scans']},
    }).execute()

    return {
        'ok': True,
        'balance': new_bal,
        'scans_added': plan['scans'],
        'scans_remaining': new_scans,
        'reference': ref,
    }




@router.get('/wallet/statement')
async def wallet_statement(authorization: str = Header(None), limit: int = 100):
    user = auth_user(authorization)
    uid = user['sub']
    s = svc()
    rows = s.table('wallet_transactions').select('*').eq('user_id', uid).order('created_at', desc=True).limit(limit).execute()
    return {'transactions': rows.data or []}


@router.get('/wallet/receipt/{reference}')
async def wallet_receipt(reference: str, authorization: str = Header(None)):
    user = auth_user(authorization)
    uid = user['sub']
    s = svc()
    r = s.table('wallet_transactions').select('*').eq('reference', reference).eq('user_id', uid).limit(1).execute()
    if not r.data or len(r.data) == 0:
        raise HTTPException(404, 'Receipt not found')
    tx = r.data[0]
    w = s.table('farmer_wallets').select('balance,account_number').eq('user_id', uid).limit(1).execute()
    wrow = w.data[0] if w.data else {}
    return {
        'receipt_number': tx.get('receipt_number'),
        'reference': tx.get('reference'),
        'type': tx.get('type'),
        'direction': tx.get('direction'),
        'amount': tx.get('amount'),
        'status': tx.get('status'),
        'created_at': tx.get('created_at'),
        'counterparty_name': tx.get('counterparty_name'),
        'counterparty_acct': tx.get('counterparty_acct'),
        'meta': tx.get('meta') or {},
        'current_balance': wrow.get('balance'),
        'account_number': wrow.get('account_number'),
        'user_email': user.get('email'),
    }
