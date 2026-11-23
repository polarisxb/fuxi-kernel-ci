from pathlib import Path
import sys,json
ROOT=Path(__file__).resolve().parent.parent

def parse(path):
 result={}
 for line in Path(path).read_text().splitlines():
  if line.startswith('CONFIG_'):
   key,value=line.split('=',1);result[key]=value
  elif line.startswith('# CONFIG_') and line.endswith(' is not set'):
   result[line.split()[1]]='n'
 return result

def validate(actual,stock,pretrim=False):
 a=parse(actual);s=parse(stock)
 required=json.loads((ROOT/'configs/required-config.json').read_text())
 failures=[{'option':key,'expected':value,'actual':a.get(key,'n')} for key,value in required.items() if a.get(key,'n')!=value]
 diff=[{'option':key,'stock':s.get(key,'n'),'built':a.get(key,'n')} for key in sorted(a.keys()|s.keys()) if a.get(key,'n')!=s.get(key,'n')]
 allowed=set(required)|{'CONFIG_UNUSED_KSYMS_WHITELIST'}
 if pretrim:allowed.add('CONFIG_TRIM_UNUSED_KSYMS')
 bad=[item for item in diff if item['option'] not in allowed]
 # Security settings in required are checked exactly, not merely allowlisted.
 if not pretrim and a.get('CONFIG_TRIM_UNUSED_KSYMS')!='y':failures.append({'option':'CONFIG_TRIM_UNUSED_KSYMS','expected':'y','actual':a.get('CONFIG_TRIM_UNUSED_KSYMS')})
 if failures or bad:raise ValueError(json.dumps({'required_failures':failures,'unexpected_drift':bad}))
 return {'phase':'sukisu','differences':diff,'unexpected':bad,'required_options':len(required)}

if __name__=='__main__':
 actual,stock,phase=sys.argv[1:4]
 if phase!='sukisu':raise SystemExit('This isolated launcher only supports the SUSFS SukiSU build.')
 print(json.dumps(validate(actual,stock,'--pretrim' in sys.argv[4:]),indent=2))
