from pathlib import Path
import subprocess,json,re,hashlib,struct
def require(condition, message):
 if not condition:
  raise ValueError(message)

ROOT=Path(__file__).resolve().parent.parent
D=ROOT/'reports/uapi';D.mkdir(parents=True,exist_ok=True)
(D/'complete-uapi-alignment.json').unlink(missing_ok=True)
KERNEL=ROOT/'kernel/KernelSU/kernel/include/uapi'
MANAGER=ROOT/'configs/manager40900-uapi'
manager_manifest=json.loads((ROOT/'configs/manager40900-uapi.json').read_text())
for name,expected in manager_manifest['sha256'].items():
 require(hashlib.sha256((MANAGER/name).read_bytes()).hexdigest()==expected, ('Manager header changed',name))
require(MANAGER.resolve()!=KERNEL.resolve(), 'UAPI self-comparison is forbidden')
CLANG=str(ROOT/'kernel/prebuilts/clang/host/linux-x86/clang-r450784e/bin/clang')
headers={}
for who in ('kernel','manager'):
 folder=D/('uapi-'+who)/'uapi';folder.mkdir(parents=True,exist_ok=True)
 data={}
 for name in ('app_profile.h','feature.h','supercall.h','selinux.h','sulog.h'):
  text=(KERNEL/name).read_text() if who=='kernel' else (MANAGER/name).read_text()
  (folder/name).write_text(text);data[name]=text
 headers[who]=data
# Compare every shared ioctl identifier and every wire command structure in supercall.h.
names=[]
for who in headers:
 text=headers[who]['supercall.h']
 constants=set(re.findall(r'\b(KSU_IOCTL_[A-Z0-9_]+)\b',text))|{'KERNEL_SU_UAPI_VERSION','KSU_APP_PROFILE_VER','KSU_INSTALL_MAGIC1','KSU_INSTALL_MAGIC2'}
 types=set(re.findall(r'^struct (ksu_\w+_cmd)\s*\{',text,re.M))|{'root_profile','app_profile','non_root_profile'}
 names.append((constants,types))
kernel_only_constants=names[0][0]-names[1][0]
manager_only_constants=names[1][0]-names[0][0]
kernel_only_types=names[0][1]-names[1][1]
manager_only_types=names[1][1]-names[0][1]
# No Manager40900 interface may be silently missing in this release.
require(not kernel_only_constants, kernel_only_constants)
require(not kernel_only_types, kernel_only_types)
require(not manager_only_constants, manager_only_constants)
require(not manager_only_types, manager_only_types)
shared_constants=names[0][0]&names[1][0]
shared_types=names[0][1]&names[1][1]
# Extract member names from all structures for offsetof checks.
fields={}
for who in headers:
 text=headers[who]['app_profile.h']+'\n'+headers[who]['supercall.h']
 table={}
 for typ in sorted(shared_types):
  pattern=re.search(r'\bstruct '+typ+r'\s*\{',text);require(pattern, (who,typ))
  pos=pattern.end();level=1;end=pos
  while level:
   if text[end]=='{':level+=1
   elif text[end]=='}':level-=1
   end+=1
  body=text[pos:end-1];body=re.sub(r'/\*.*?\*/|//[^\n]*','',body,flags=re.S)
  if typ in ('root_profile','app_profile'):
   member={'root_profile':['uid','gid','groups_count','groups','capabilities','capabilities.effective','capabilities.permitted','capabilities.inheritable','selinux_domain','namespaces','flags'],'app_profile':['version','key','allow_su','rp_config','rp_config.use_default','rp_config.template_name','rp_config.profile','nrp_config','nrp_config.use_default','nrp_config.profile']}[typ]
   if typ=='app_profile':member.append('curr_uid')
  else:
   member=re.findall(r'\b([A-Za-z_][A-Za-z_0-9]*)\s*(?:\[[^]]*\])?\s*;',body)
  table[typ]=member
 fields[who]=table
require(fields['kernel']==fields['manager'], ('field-list mismatch',fields))
source='#include <stdio.h>\n#include <stddef.h>\n#include <stdbool.h>\n#define __user\n#include "uapi/supercall.h"\nint main(void) {\n'
expressions={}
for const in sorted(shared_constants):
 source+=f'printf("constant.{const}=%llu\\n", (unsigned long long){const});\n'
 expressions['constant.'+const]=const
for typ in sorted(shared_types):
 source+=f'printf("sizeof.{typ}=%zu\\n",sizeof(struct {typ}));\n'
 expressions['sizeof.'+typ]=f'sizeof(struct {typ})'
 for member in fields['kernel'][typ]:
  source+=f'printf("offset.{typ}.{member}=%zu\\n",offsetof(struct {typ},{member}));\n'
  expressions[f'offset.{typ}.{member}']=f'offsetof(struct {typ},{member})'
source+='return 0;}\n'
outputs={}
for who in ('kernel','manager'):
 folder=D/('uapi-'+who);(folder/'layout.c').write_text(source)
 p=subprocess.run([CLANG,'-Wall','-Wextra','-Werror','-I',str(folder),str(folder/'layout.c'),'-o',str(folder/'layout')],capture_output=True,text=True)
 require(p.returncode==0, p.stderr)
 output=subprocess.check_output([str(folder/'layout')],text=True);(folder/'layout.txt').write_text(output)
 outputs[who]={k:int(v) for line in output.splitlines() for k,v in [line.split('=',1)]}
require(outputs['kernel']==outputs['manager'], {k:(outputs['kernel'].get(k),outputs['manager'].get(k)) for k in outputs['kernel'].keys()|outputs['manager'].keys() if outputs['kernel'].get(k)!=outputs['manager'].get(k)})
# Compare every value for the actual arm64 ABI by extracting an ELF data
# section; this needs neither executing phone code nor emulating ARM.
arm64_outputs={}
for who in ('kernel','manager'):
 folder=D/('uapi-'+who)
 probe='#include <stddef.h>\n#include <stdbool.h>\n#define __user\n#include "uapi/supercall.h"\n'
 probe+='extern "C" { __attribute__((used,section(".ksu_abi"))) const unsigned long long values[] = {\n'
 probe+=',\n'.join('(unsigned long long)('+expr+')' for expr in expressions.values())+'\n}; }\n'
 (folder/'arm64.cc').write_text(probe)
 sysroot=ROOT/'kernel/prebuilts/ndk-r23/toolchains/llvm/prebuilt/linux-x86_64/sysroot'
 p=subprocess.run([CLANG+'++','--target=aarch64-linux-android31','--sysroot='+str(sysroot),'-std=c++17','-Wall','-Wextra','-Werror','-I',str(folder),'-Xclang','-fdump-record-layouts','-c',str(folder/'arm64.cc'),'-o',str(folder/'arm64.o')],capture_output=True,text=True)
 require(p.returncode==0, p.stderr)
 (folder/'arm64-record-layouts.txt').write_text(p.stdout+p.stderr)
 subprocess.run([str(Path(CLANG).with_name('llvm-objcopy')),'--dump-section','.ksu_abi='+str(folder/'arm64-values.bin'),str(folder/'arm64.o')],check=True,capture_output=True)
 vector=struct.unpack('<'+'Q'*len(expressions),(folder/'arm64-values.bin').read_bytes())
 arm64_outputs[who]=dict(zip(expressions,vector))
 require(arm64_outputs[who]==outputs[who], ('host vs arm64 mismatch',who))
 (folder/'arm64-values.json').write_text(json.dumps(arm64_outputs[who],indent=2)+'\n')
require(arm64_outputs['kernel']==arm64_outputs['manager'], 'arm64 UAPI mismatch')
lines=[]
for key in sorted(outputs['kernel']):
 if 'get_info_cmd' in key or key.endswith(('VERSION','VER')) or key.startswith('sizeof.app_profile'):lines.append({key:outputs['kernel'][key]})
result={'manager_sha':'904c60d18f026340ce38ca2d5a1da529aadcba1a','kernel_sha':'e2912817f4e1b194e582a06e0b5eacf6a3fb7083','compared_shared_ioctl_constants':sum(n.startswith('KSU_IOCTL_') for n in shared_constants),'compared_shared_wire_structures':len(shared_types),'compared_constants_sizes_offsets':len(outputs['kernel']),'shared_interfaces_all_equal':True,'all_manager_header_ioctls_present':True,'manager_only_ioctl_constants':sorted(manager_only_constants),'manager_only_structures':sorted(manager_only_types),'runtime_note':'Header presence is checked; runtime feature functionality remains untested.','kernel_and_manager_uapi':outputs['kernel']['constant.KERNEL_SU_UAPI_VERSION'],'profile_version':outputs['kernel']['constant.KSU_APP_PROFILE_VER'],'arm64_values_all_equal':True,'target':'aarch64-linux-android31','selected':lines,'header_sha256':{who:{name:hashlib.sha256(text.encode()).hexdigest() for name,text in data.items()} for who,data in headers.items()},'note':'Independent official Manager40900 header comparison; this build has no runtime authorization or feature validation claim.'}
(D/'complete-uapi-alignment.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
