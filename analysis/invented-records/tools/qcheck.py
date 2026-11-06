# Copied verbatim from DATA-2026-0001 (9b098265b7c0cc2f487b22aaba134528bf1a505a),
# candidate-interview-2026-10/analysis/tools/qcheck.py: the quote locator
# its counters used. Kept here for attribution and comparison; it reads
# DATA-2026-0001's layout. This study's port (the same norm(), frags() and
# quote extraction, its corpus the case's inputs) is
# src/council_diversity/invrec.py, run as
#   uv run scripts/invented_records.py qcheck <case-id> ...
# tests/test_invented_records.py checks the port against this file.
import sys, re, os, difflib
REPO='.'  # the agora repository checkout (was a local absolute path)
ASKS='asks'  # this data set's asks/
def norm(s):
    s=s.replace('’',"'").replace('‘',"'").replace('“','"').replace('”','"')
    s=s.replace('—','-').replace('–','-').replace('—','-').replace('§','s ')
    s=re.sub(r'[*_`>#]','',s)
    s=re.sub(r'\\','',s)
    s=s.lower()
    s=re.sub(r'[^a-z0-9\'\- ]+',' ',s)
    s=re.sub(r'\s+',' ',s).strip()
    return s
base=open(REPO+'/constitution.md').read()+'\n'+open(REPO+'/governance-protocol.md').read()
def corpus_for(path):
    fn=os.path.basename(path)[:-3]
    case,arm=fn.split('__')
    ask=open(f'{ASKS}/{arm}/{case}.md').read()
    return norm(base+'\n'+ask), base+ask
def frags(q):
    parts=re.split(r'\.\.\.|…|\[\.\.\.\]|\[…\]',q)
    return [p for p in parts if len(norm(p).split())>=3]
def check(path):
    C,raw=corpus_for(path)
    txt=open(path).read()
    samples=re.split(r'^### (response-sample-\d+)\.json.*$',txt,flags=re.M)
    out=[]
    for i in range(1,len(samples),2):
        name=samples[i]; body=samples[i+1]
        quotes=[]
        for m in re.finditer(r'["“]([^"“”\n]{12,600})["”]',body):
            quotes.append(('q',m.group(1)))
        # blockquote groups
        bq=[]
        for line in body.split('\n')+['']:
            if line.startswith('>'):
                bq.append(line.lstrip('> ').strip())
            elif bq:
                quotes.append(('bq',' '.join(bq))); bq=[]
        for kind,q in quotes:
            fs=frags(q)
            if not fs: continue
            bad=[f for f in fs if norm(f) not in C]
            if bad:
                # closest match
                nb=norm(bad[0]); words=nb.split()
                best=''
                # find best anchor using first 4 words
                key=' '.join(words[:4]); idx=C.find(key)
                if idx<0:
                    key=' '.join(words[-4:]); idx=C.find(key)
                    if idx>=0: idx=max(0,idx-len(nb))
                if idx>=0: best=C[idx:idx+len(nb)+40]
                out.append(f'{name} [{kind}] MISS: {q[:400]}\n      nearest: {best[:300]}')
    return out
for p in sys.argv[1:]:
    print('=====',os.path.basename(p))
    for l in check(p): print(l)
