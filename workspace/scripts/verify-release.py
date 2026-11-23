#!/usr/bin/env python3
"""Check the actual builtin release, dispatch permissions and SUSFS hook ownership."""
from pathlib import Path
import hashlib,json,re,sys
ROOT=Path(__file__).resolve().parent.parent
KERNEL=ROOT/'kernel/KernelSU/kernel'


def verify():
 make=(KERNEL/'Makefile').read_text()
 number=int(re.search(r'^KSU_VERSION\s*:=\s*(\d+)',make,re.M).group(1))
 full=re.search(r'^KSU_VERSION_FULL\s*:=\s*(\S+)',make,re.M).group(1)
 if number!=40900 or not full.startswith('v4.2.0-builtin-e2912817-susfs-local'):
  raise ValueError('Unexpected driver version/source identity')
 dispatch=(KERNEL/'supercall/dispatch.c').read_text()
 entries=re.findall(r'\.cmd\s*=\s*(KSU_IOCTL_\w+),.*?\.handler\s*=\s*(\w+),.*?\.perm_check\s*=\s*(\w+)',dispatch,re.S)
 expected=json.loads((ROOT/'configs/manager40900-ioctl-permissions.json').read_text())
 actual={cmd:permission for cmd,handler,permission in entries}
 if len(entries)!=31 or actual!=expected:raise ValueError('Manager40900 ioctl coverage/permissions differ: '+str(actual))
 sources='\n'.join(p.read_text() for p in KERNEL.rglob('*.c'))
 for cmd,handler,permission in entries:
  if not re.search(r'\b(?:int|long)\s+'+re.escape(handler)+r'\s*\([^;]*?\)\s*\{',sources,re.S):
   raise ValueError(f'Missing implementation for {cmd}: {handler}')
 susfs_control=dispatch.split('int ksu_handle_sys_reboot(',1)[1].split('switch(cmd)',1)[0] if 'int ksu_handle_sys_reboot(' in dispatch else ''
 if not re.search(r'current_uid\(\)\.val\s*==\s*0',susfs_control):raise ValueError('SUSFS control must retain UID0 restriction')
 if 'susfs_init();' not in (KERNEL/'ksu.c').read_text():raise ValueError('SUSFS initialization missing')
 lsm=(KERNEL/'hook/lsm_hook.c').read_text()
 if not re.search(r'#ifndef CONFIG_KSU_SUSFS\s*LSM_HOOK_INIT\(task_fix_setuid',lsm):
  raise ValueError('SUSFS setuid hook must exclude duplicate ordinary LSM registration')
 if (KERNEL/'hook/syscall_hook_manager.c').exists():raise ValueError('Native tracepoint hook architecture must not be mixed with builtin SUSFS inline hooks')
 apk_sign=(KERNEL/'manager/apk_sign.c').read_text()
 expected_hash='947ae944f3de4ed4c21a7e4f7953ecf351bfa2b36239da37a34111ad29993eef'
 if '{ 0x35c, "'+expected_hash+'" }' not in apk_sign:raise ValueError('Official Manager signature identity missing')
 common=ROOT/'kernel/common'
 if not (common/'drivers/kernelsu').is_symlink() or (common/'drivers/kernelsu').resolve()!=KERNEL.resolve():raise ValueError('Driver integration symlink absent or wrong')
 features=json.loads((ROOT/'configs/required-config.json').read_text())
 result={'passed':True,'kernel_source':'e2912817f4e1b194e582a06e0b5eacf6a3fb7083','version':number,'full_version':full,'manager_version':40900,'upstream_version_formula':'40000+3715(main904c60d1)-2815','ioctl_handlers':len(entries),'ioctl_permissions':actual,'kpm_configuration':'disabled; handler remains conditional as upstream','susfs_root_only_control':True,'hook_architecture':'builtin SUSFS inline; ordinary setuid LSM registration excluded','susfs_features_required':[k for k,v in features.items() if k.startswith('CONFIG_KSU_SUSFS') and v=='y'],'runtime_verified':False}
 (ROOT/'reports/release-contract.json').write_text(json.dumps(result,indent=2)+'\n')
 print(json.dumps(result,indent=2))
 return 0

if __name__=='__main__':
 try:sys.exit(verify())
 except Exception as error:
  print(str(error),file=sys.stderr);sys.exit(1)
