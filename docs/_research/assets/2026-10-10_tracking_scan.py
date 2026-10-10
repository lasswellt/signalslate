"""Scan mail bodies for every tracking-number format in jkeen/tracking_number_data plus extras.
Usage: scan.py <jkeen_dir> <bodies.jsonl> <out_prefix>"""
import base64, glob, json, re, sys, urllib.parse
from collections import Counter, defaultdict

D, BODIES, OUT = sys.argv[1:4]

def conv(r):
    seen = {}
    def rep(m):
        n = m.group(1); seen[n] = seen.get(n, 0) + 1
        return '(?P<%s>' % (n if seen[n] == 1 else '%s__%d' % (n, seen[n]))
    r = re.sub(r'\(\?<(\w+)>', rep, r)
    return r.replace(r'\s*', '')          # compact form: scan tokens, not whitespace-spread text

def digits(s): return [int(c) if c.isdigit() else (ord(c) - 3) % 10 for c in s]
def mod10(ser, chk, v):
    s = ser[::-1] if v.get('reverse') else ser
    t = sum(x * (v.get('odds_multiplier', 1) if i % 2 else v.get('evens_multiplier', 1)) for i, x in enumerate(digits(s)))
    c = t % 10; return (0 if c == 0 else 10 - c) == chk
def wmod(ser, chk, v):
    t = sum(a * b for a, b in zip(digits(ser), v['weightings'])); return t % v['modulo1'] % v['modulo2'] == chk
def mod7(ser, chk, v): return int(ser) % 7 == chk
def s10(ser, chk, v):
    t = sum(a * b for a, b in zip(map(int, ser), [8, 6, 4, 2, 3, 5, 9, 7])); c = 11 - t % 11
    return {10: 0, 11: 5}.get(c, c) == chk
def luhn(ser, chk, v):
    t = 0
    for i, x in enumerate(reversed(digits(ser))):
        x = x * 2 if i % 2 == 0 else x
        t += x - 9 if x > 9 else x
    return (10 - t % 10) % 10 == chk
CK = {'mod10': mod10, 'sum_product_with_weightings_and_modulo': wmod, 'mod7': mod7, 's10': s10, 'luhn': luhn}

RULES = []
for f in sorted(glob.glob(D + '/couriers/*.json')):
    d = json.load(open(f))
    for t in d['tracking_numbers']:
        r = t['regex'] if isinstance(t['regex'], str) else ''.join(t['regex'])
        RULES.append((d['courier_code'] if 'courier_code' in d else d['name'], t['name'],
                      re.compile(r'(?<![A-Za-z0-9])' + conv(r) + r'(?![a-z0-9])'), re.compile('^' + conv(r) + '$'),
                      t.get('validation', {})))
# extras not in jkeen
EXTRA = [('walmart', 'Walmart last-mile 555', re.compile(r'(?<!\d)555\d{17}(?!\d)'))]

def classify(tok):
    out = []
    for car, name, _, full, v in RULES:
        m = full.match(tok)
        if not m: continue
        ck = v.get('checksum')
        if not ck: out.append((car, name, None)); continue
        g = m.groupdict(); ser = g.get('SerialNumber') or ''; cd = g.get('CheckDigit')
        fn = CK.get(ck['name'])
        out.append((car, name, fn(ser, int(cd), ck) if fn and cd and cd.isdigit() else None))
    return out

LABEL = re.compile(r'(tracking\s*(number|no\.?|#|id)?|track(ing)?\s*#|waybill( number)?|parcel number|tracking id|track your package)\s*[:#|]*\s*(with\s+)?$', re.I)
ORDERLBL = re.compile(r'(order\s*(number|no\.?|#|id)?|shipment\s*#|customer\s*no\.?|reference(\s*id)?|invoice|item(\s*id)?|sku|internet\s*#|confirmation\s*#?|phone|call|text|reach me at|member|account)\s*[:#|\[\]()+]*\s*$', re.I)
LABELED_VALUE = re.compile(r'(?i:tracking\s*(?:number|no\.?|#|id)|track(?:ing)?\s*#|waybill(?:\s*number)?|parcel number|track your package|pro\s*(?:number|no\.?|#)|pronumber)(?:\s*(?i:for these items is))?\s*[:#|]*\s*((?:[A-Z0-9]|[ -](?=\d)){8,40}?)(?=[^A-Z0-9]|[A-Z][a-z]|$)')
URLPARAMS = re.compile(r'(?:tracknum|tracknums|tracking_numbers?|trackingnumber|trknbr|tlabels|nums|tracking-id|trackingid|awb)=([A-Za-z0-9,%]{8,40})', re.I)
HOSTID = re.compile(r'(?:swiship\.com|amazon\.com/progress-tracker)[^\s]*?[?&]id=([A-Za-z0-9]{10,20})', re.I)

def decode_urls(text):
    urls = re.findall(r'https?://[^\s)\]"\'<>]+', text)
    out = []
    for u in urls:
        cand = {u, urllib.parse.unquote(u), urllib.parse.unquote(urllib.parse.unquote(u))}
        for p in re.findall(r'[?&]p=([A-Za-z0-9_\-=]+)', u):        # Mandrill p= base64 JSON
            try:
                j = base64.urlsafe_b64decode(p + '=' * (-len(p) % 4)).decode('utf8', 'ignore'); cand.add(urllib.parse.unquote(j))
            except Exception: pass
        out.extend(cand)
    return out

def strip_urls(text): return re.sub(r'https?://[^\s)\]"\'<>]+', ' ', text)

stats = Counter(); found = []; labeled_unmatched = []
for line in open(BODIES):
    m = json.loads(line); mid, sender, subj = m['id'], m.get('sender', ''), m.get('subject', '')
    body = m.get('body') or ''
    text = subj + '\n' + strip_urls(body)
    urls = decode_urls(body)
    cands = {}  # tok -> set(sources)
    # 1. every jkeen pattern over visible text
    for car, name, rx, _, _ in RULES:
        for mm in rx.finditer(text):
            cands.setdefault(mm.group(0), set()).add('text')
    for car, name, rx in EXTRA:
        for mm in rx.finditer(text): cands.setdefault(mm.group(0), set()).add('text')
    # 2. URL params (incl. decoded wrappers)
    for u in urls:
        for v in URLPARAMS.findall(u):
            for tok in urllib.parse.unquote(v).split(','):
                if len(tok) >= 8: cands.setdefault(tok.upper() if tok[:2].lower() in ('1z', 'tb', 'yt') else tok, set()).add('url')
        for v in HOSTID.findall(u): cands.setdefault(v, set()).add('url')
        for mm in re.finditer(r'/(usps|ups|fedex|dhl)/([A-Za-z0-9]{10,34})', u, re.I):
            cands.setdefault(mm.group(2), set()).add('url-path')
    # 3. labeled values (catch formats jkeen does not know)
    for mm in LABELED_VALUE.finditer(text):
        tok = re.sub(r'[\s\-]', '', mm.group(1))
        if not re.search(r'\d', tok) or (tok.isdigit() and len(tok) < 9): continue
        cands.setdefault(tok, set()).add('label')
    for tok, srcs in cands.items():
        res = classify(tok)
        if tok.startswith('555') and len(tok) == 20 and tok.isdigit(): res = [('walmart', 'Walmart last-mile 555', None)] + res
        idx = text.find(tok); pre = text[max(0, idx - 60):idx] if idx >= 0 else ''
        ctx_label = bool(LABEL.search(pre)) or 'label' in srcs or 'url' in srcs or 'url-path' in srcs
        ctx_order = bool(ORDERLBL.search(pre))
        passed = [r for r in res if r[2] is True]
        WEAK = {'DHL Express', 'Purolator (12)'}
        ctxt = (pre + ' ' + sender + ' ' + ' '.join(srcs)).lower()
        strong = [r for r in passed if r[1] not in WEAK or (r[0] in ctxt or 'waybill' in ctxt or 'dhl' in ctxt and r[0] == 'dhl')]
        passed = strong
        nock = [r for r in res if r[2] is None]
        if 'walmart' in sender.lower() and res and res[0][0] == 'walmart': verdict = 'VALID:walmart(sender-rule)'
        elif passed and not ctx_order: verdict = 'VALID:' + passed[0][0] + '/' + passed[0][1]
        elif nock and ctx_label and not ctx_order: verdict = 'VALID:' + nock[0][0] + '/' + nock[0][1] + '(label,no-checksum)'
        elif not res and 'label' in srcs: verdict = 'UNKNOWN-FORMAT(labeled)'
        elif passed and ctx_order: verdict = 'REJECT:order-labeled(passed ' + passed[0][1] + ')'
        elif nock: verdict = 'REJECT:no-checksum,no-label(' + nock[0][1] + ')'
        elif res: verdict = 'REJECT:checksum-fail(' + res[0][1] + ')'
        else: verdict = 'REJECT:no-match'
        found.append(dict(id=mid, sender=sender, tok=tok, src=sorted(srcs), verdict=verdict, pre=pre[-40:].replace('\n', ' ')))
        head, _, rest = verdict.partition(':')
        stats[head + (':' + re.split(r'[/(]', rest)[0] if rest else '')] += 1

json.dump(found, open(OUT + '_found.json', 'w'), indent=1)
print('messages scanned:', sum(1 for _ in open(BODIES)))
for k, v in sorted(stats.items(), key=lambda x: -x[1]): print(f'{v:5d}  {k}')
