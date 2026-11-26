#!/usr/bin/env python3
"""Fail before compilation when a locked input, signing pair or tool is absent."""
from pathlib import Path
import hashlib,json,os,shutil,subprocess,sys
sys.dont_write_bytecode = True
from build_provenance import BASELINE_CERT_SHA256, check_signing_certificate
ROOT=Path(__file__).resolve().parent.parent
SIGNING=Path(os.environ.get('SIGNING_KEY_DIR', ROOT/'inputs/signing'))

def digest(path):
 h=hashlib.sha256()
 with path.open('rb') as stream:
  for block in iter(lambda:stream.read(4*1024*1024),b''):h.update(block)
 return h.hexdigest()

def run(args,input=None):
 p=subprocess.run([str(x) for x in args],input=input,capture_output=True,timeout=30)
 if p.returncode:raise ValueError(f'{args[0]} failed: {p.stderr.decode(errors="replace")[:500]}')
 return p.stdout

def verify():
 errors=[];checked=[]
 def check(label,fn):
  try:checked.append({'input':label,'result':fn()})
  except Exception as exc:errors.append({'input':label,'error':str(exc)})
 for tool in ('python3','make','git','openssl','rsync','bc','bison','flex','cpio','modprobe','modinfo','depmod','flock','patch'):
  def executable(tool=tool):
   path=shutil.which(tool)
   if not path:raise ValueError('not found in PATH')
   return path
  check('tool:'+tool,executable)
 for entry in json.loads((ROOT/'configs/locked-workspace-inputs.json').read_text()):
  def revision(entry=entry):
   path=ROOT/'kernel'/entry['path']
   actual=run(['git','-C',path,'rev-parse','HEAD']).decode().strip()
   if actual!=entry['sha']:raise ValueError(f'expected {entry["sha"]}, got {actual}')
   if entry.get('shared_readonly'):
    dirty=run(['git','-C',path,'status','--porcelain','--untracked-files=all']).decode()
    if dirty:raise ValueError('shared dependency has uncommitted changes')
   return actual
  check(entry['path'],revision)
 for name,expected in json.loads((ROOT/'configs/tool-input-sha256.json').read_text()).items():
  def content(name=name,expected=expected):
   actual=digest(ROOT/'kernel'/name)
   if actual!=expected:raise ValueError(f'content hash changed: {actual}')
   return actual
  check(name,content)
 def signing():
  key=SIGNING/'signing_key.pem';cert=SIGNING/'signing_key.x509'
  if not key.is_file() or not cert.is_file():raise ValueError('required external signing key/certificate missing')
  check_signing_certificate(digest(cert), BASELINE_CERT_SHA256)
  public_key=run(['openssl','pkey','-in',key,'-pubout','-outform','DER'])
  cert_public=run(['openssl','x509','-inform','DER','-in',cert,'-pubkey','-noout'])
  cert_der=run(['openssl','pkey','-pubin','-outform','DER'],input=cert_public)
  if public_key!=cert_der:raise ValueError('private key and public certificate do not match')
  return {'public_certificate_sha256':digest(cert),'matching_key':True}
 check('external_signing_pair',signing)
 def driver_link():
  path=ROOT/'kernel/common/drivers/kernelsu'
  if not path.is_symlink() or path.resolve()!=(ROOT/'kernel/KernelSU/kernel').resolve():raise ValueError('missing or wrong KernelSU integration symlink')
  return os.readlink(path)
 check('drivers/kernelsu',driver_link)
 def compiler():
  text=run([ROOT/'kernel/prebuilts/clang/host/linux-x86/clang-r450784e/bin/clang','--version']).decode()
  if '14.0.7' not in text or 'r450784e' not in text:raise ValueError('unexpected compiler version')
  return text.splitlines()[0]
 check('compiler_identity',compiler)
 def lto_support():
  text=run([ROOT/'kernel/prebuilts/clang/host/linux-x86/clang-r450784e/bin/ld.lld','--help']).decode()
  if '--threads=' not in text or '--lto-partitions=' not in text:raise ValueError('required Full LTO parallel codegen flags unsupported')
  return 'Full LTO threads/partitions supported'
 check('linker_parallel_codegen',lto_support)
 report={'passed':not errors,'inputs':checked,'errors':errors,'phone_operations':False}
 (ROOT/'reports').mkdir(exist_ok=True)
 (ROOT/'reports/build-preflight.json').write_text(json.dumps(report,indent=2)+'\n')
 print(json.dumps({'passed':not errors,'checked':len(checked),'errors':errors},indent=2))
 return 0 if not errors else 1
if __name__=='__main__':sys.exit(verify())
