# .github/workflows/productbot-metrics.yml
name: ProductBot Metrics
on:
  schedule: [ { cron: "15 5 * * *" } ]  # 01:15 ET
  workflow_dispatch: {}
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install tweepy
      - name: Harvest metrics
        run: |
          python - << 'PY'
import csv, os, json, time
from datetime import datetime, timezone, timedelta
import tweepy
LOG = "bots/logs/tweet_logs.csv"
MET = "bots/logs/metrics.csv"
STATE = "bots/state/bandit.json"
api = tweepy.Client(
  consumer_key=os.environ["TWITTER_API_KEY"],
  consumer_secret=os.environ["TWITTER_API_SECRET"],
  access_token=os.environ["TWITTER_ACCESS_TOKEN"],
  access_token_secret=os.environ["TWITTER_ACCESS_SECRET"]
)
def load_bandit():
    try:
        return json.load(open(STATE,"r",encoding="utf-8"))
    except: 
        return {}
bandit = load_bandit()
rows=[]
with open(LOG,encoding="utf-8") as f:
  rdr = csv.DictReader(f)
  for r in rdr: rows.append(r)
recent = rows[-40:]  # last 40 thread posts
ids=set()
for r in recent:
  if r["status"].startswith("success"):
    ids.update([r["tweet_id_1"], r["tweet_id_2"]])
if not ids: raise SystemExit
chunks=[list(ids)[i:i+100] for i in range(0,len(ids),100)]
allm=[]
for ch in chunks:
  res = api.get_tweets(ids=ch, tweet_fields=["public_metrics"])
  for t in res.data or []:
    m=t.data["public_metrics"]
    allm.append((t.id, m["like_count"], m["reply_count"], m["retweet_count"], m.get("quote_count",0)))
with open(MET,"a",newline="",encoding="utf-8") as f:
  w=csv.writer(f)
  ts=datetime.now(timezone.utc).isoformat(timespec="seconds")
  for tid,l,r,rt,q in allm:
    w.writerow([ts,tid,l,r,rt,q])
# simple credit back to modes by tweet_id_1
byid={r["tweet_id_1"]:r for r in recent}
rewards={}
for tid,l,r,rt,q in allm:
  if tid in byid:  # primary tweet reward
    mode=byid[tid]["mode"]
    rewards.setdefault(mode,0)
    rewards[mode]+= l + 2*r + 2*rt + q
for mode,score in rewards.items():
  st=bandit.get(mode, {"w":1.0,"n":0,"r":0.0})
  st["n"]+=1; st["r"]+=score; st["w"]=max(0.2, st["r"]/st["n"])
  bandit[mode]=st
json.dump(bandit, open(STATE,"w",encoding="utf-8"), indent=2)
print("Updated bandit:", bandit)
PY
        env:
          TWITTER_API_KEY: ${{ secrets.TWITTER_API_KEY }}
          TWITTER_API_SECRET: ${{ secrets.TWITTER_API_SECRET }}
          TWITTER_ACCESS_TOKEN: ${{ secrets.TWITTER_ACCESS_TOKEN }}
          TWITTER_ACCESS_SECRET: ${{ secrets.TWITTER_ACCESS_SECRET }}
