"""
debug_keyword_categories.py
Pulls all unique tokens from product names and shows which fall into
Colour / Material / Detail categories.

Run on Railway:  python debug_keyword_categories.py
"""
import sys, os, re, sqlite3
from collections import Counter

# ── Locate DB ─────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
try:
    from config import DATABASE_PATH
except Exception:
    DATABASE_PATH = os.path.join(os.path.dirname(__file__), 'mrd_tracker.db')

# ── Term lists ────────────────────────────────────────────────────────────────

COLOURS = {
    'black','white','grey','gray','cream','beige','ivory','nude','natural',
    'stone','oatmeal','ecru','off white',
    'brown','tan','camel','chocolate','cognac','chestnut','tobacco',
    'pink','red','burgundy','wine','berry','blush','rose','coral','fuchsia',
    'hot pink','pale pink','dusty pink','bright red','deep red','dark red',
    'navy','blue','teal','mint','green','khaki','olive','sage',
    'royal blue','light blue','dark blue','army green','forest green',
    'olive green','sage green',
    'yellow','orange','mustard','rust','amber','copper',
    'purple','lilac','lavender','mauve','violet',
    'silver','gold','bronze','metallic','rose gold','gunmetal',
    'multicolour','multicolor','multi','leopard','zebra','snake','animal',
    'floral','tropical','camo','camouflage','tortoiseshell','printed',
    'clear','transparent','neon','pastel',
    'light grey','dark grey','light brown','dark brown','light pink',
    'bright white','dark navy',
}

MATERIALS = {
    'leather','suede','nubuck','patent',
    'faux leather','faux suede','real leather','real suede',
    'genuine leather','vegan leather','pu leather','patent leather',
    'faux patent','suedette',
    'canvas','denim','fabric','textile','woven','linen','cotton',
    'jersey','knit','knitted','crochet',
    'mesh','neoprene','rubber','synthetic','lycra','elastic','elasticated',
    'foam','eva','memory foam',
    'satin','velvet','silk','lace','sequin','glitter','shimmer',
    'raffia','jute','cork','wood','wooden',
    'fur','faux fur','fluffy','shearling','teddy',
    'wicker','straw',
    'faux snakeskin','snakeskin','croc','crocodile','faux croc',
    'croc effect','snake print','leopard print',
}

DETAILS = {
    'pointed','round','square','almond',
    'pointed toe','round toe','square toe','open toe','closed toe',
    'block heel','kitten heel','cone heel','stiletto','wedge heel',
    'block','kitten','cone','platform','flatform','wedge',
    'chunky','stacked','tapered','low heel','high heel','mid heel',
    'lace up','slip on','buckle','velcro','zip','elasticated',
    'ankle strap','t bar','mary jane','crossover','strappy','slingback',
    'ankle','knee high','thigh high','high top','low top',
    'bow','tassel','stud','studded','embellished','embroidered',
    'quilted','perforated','cutout','cut out','fringed','fringe',
    'chain','ring','crystal','rhinestone','metallic trim','hardware',
    'wide fit','extra wide','narrow',
    'backless','caged','gladiator','western','cowboy','chelsea',
    'combat','hiking','running',
    'platform sole','chunky sole','lug sole','cleated','ridged',
    'peep toe','open back','low cut',
    'mule','double strap',
}

# ── Stop words ────────────────────────────────────────────────────────────────
STOP = {
    'a','an','the','and','or','in','on','with','for','to','of','at','by',
    'from','up','as','is','it','its','be','are','was','were','has','have',
    'had','do','does','did','but','not','no','so','if','this','that',
    'these','those','s','amp',
}

def tokenise(name):
    text  = name.lower().replace('-',' ').replace("'s",'').replace("'",'')
    words = re.findall(r'[a-z]+', text)
    words = [w for w in words if w not in STOP and len(w) > 1]
    tokens = list(words)
    for i in range(len(words)-1):
        tokens.append(f'{words[i]} {words[i+1]}')
    return tokens

# ── Load product names ────────────────────────────────────────────────────────
conn = sqlite3.connect(DATABASE_PATH)
rows = conn.execute("SELECT name FROM products WHERE name IS NOT NULL").fetchall()
conn.close()
print(f'Products loaded: {len(rows)}')

all_tokens: Counter = Counter()
for (name,) in rows:
    all_tokens.update(tokenise(name or ''))

unique = sorted(all_tokens.keys())
print(f'Unique tokens:   {len(unique)}\n')

# ── Match ─────────────────────────────────────────────────────────────────────
rows_out = []
for tok in unique:
    c = tok in COLOURS
    m = tok in MATERIALS
    d = tok in DETAILS
    if c or m or d:
        rows_out.append((tok, all_tokens[tok], c, m, d))

rows_out.sort(key=lambda r: (-sum(r[2:]), -r[1]))

hdr = f'{"Keyword":<38} {"Count":>5}  {"Colour":^7}  {"Material":^8}  {"Detail":^6}'
print(hdr)
print('-' * len(hdr))
for tok, cnt, c, m, d in rows_out:
    print(f'{tok:<38} {cnt:>5}  {"✓" if c else "":^7}  {"✓" if m else "":^8}  {"✓" if d else "":^6}')

print(f'\nMatched {len(rows_out)} of {len(unique)} unique tokens')

# High-freq single-word tokens that matched nothing (gaps check)
unmatched = [(t, all_tokens[t]) for t in unique
             if t not in COLOURS and t not in MATERIALS and t not in DETAILS
             and all_tokens[t] >= 3 and ' ' not in t]
unmatched.sort(key=lambda x: -x[1])
print(f'\n=== Unmatched single-word tokens (count ≥ 3, not categorised) ===')
for tok, cnt in unmatched[:60]:
    print(f'  {tok:<32} {cnt}')
