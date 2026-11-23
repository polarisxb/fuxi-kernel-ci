from pathlib import Path
import subprocess,tempfile,json,re,sys
r=Path(__file__).resolve().parent.parent
common=r/'kernel/common';out=r/'out/sukisu/common';ksu=r/'kernel/KernelSU'
cc=r/'kernel/prebuilts/clang/host/linux-x86/clang-r450784e/bin/clang'
results=[]
with tempfile.TemporaryDirectory(prefix='fuxi194-contract-') as temp:
 t=Path(temp);(t/'include/linux').mkdir(parents=True)
 header=(common/'include/linux/sukisu_hooks.h').read_text()
 (t/'include/linux/sukisu_hooks.h').write_text(header)
 flags=[str(cc),'--target=aarch64-linux-gnu','-fsyntax-only','-D__KERNEL__','-nostdinc','-isystem',str(cc.parent.parent/'lib64/clang/14.0.7/include')]
 for p in (t/'include',common/'arch/arm64/include',out/'arch/arm64/include/generated',common/'include',out/'include',common/'arch/arm64/include/uapi',out/'arch/arm64/include/generated/uapi',common/'include/uapi',out/'include/generated/uapi'):flags+=['-I',str(p)]
 flags+=['-include',str(common/'include/linux/kconfig.h')]
 cases={'valid':'', 'wrong_bool':'extern struct static_key_true ksu_su_compat_enabled;','wrong_faccessat':'int ksu_handle_faccessat(int *, const char **, int *, int *);','wrong_stat':'int ksu_handle_stat(int *, const char **, int *);','wrong_read':'void ksu_handle_sys_read(unsigned int);'}
 for name,body in cases.items():
  p=t/(name+'.c');p.write_text('#include <linux/sukisu_hooks.h>\n'+body+'\n')
  result=subprocess.run(flags+[str(p)],capture_output=True,text=True)
  expected=name=='valid'
  assert (result.returncode==0)==expected,(name,result.stderr)
  results.append({'test':name,'passed':True,'expected_compile_success':expected,'compiler_output':result.stderr})
 # Exercise the actual adapted source, not a nonexistent Git checkout.
 source=(ksu/'kernel/feature/sucompat.c').read_text();start=source.index('int ksu_handle_faccessat(');end=source.index('\n#else\n__attribute__((hot))',start)
 functions=source[start:end]
 harness=r'''#include <assert.h>
#include <stdint.h>
#include <string.h>
#include <stddef.h>
#define unlikely(x) (x)
#define likely(x) (x)
#define IS_ERR_OR_NULL(p) (!(p) || (uintptr_t)(p) >= (uintptr_t)-4095)
#define pr_info(...) ((void)0)
#define pr_err(...) ((void)0)
struct filename { const char *name; };
static const char su_path[]="/system/bin/su";
static const char sh_path[]="/system/bin/sh";
static int chrooted;
static int current_chrooted(void) { return chrooted; }
'''+functions+r'''
static void check(int stat_hook) {
 int fd=0,mode=0,flags=0;
 char name[64]="/system/bin/su";
 struct filename f={name},*p=&f;
 #define CALL(x) (stat_hook ? ksu_handle_stat(&fd,(x),&flags) : ksu_handle_faccessat(&fd,(x),&mode,&flags))
 assert(CALL(NULL)==0);
 p=NULL;assert(CALL(&p)==0);
 p=(void *)(intptr_t)-14;assert(CALL(&p)==0);
 f.name=NULL;p=&f;assert(CALL(&p)==0);
 f.name=name;chrooted=1;assert(CALL(&p)==0);assert(!strcmp(name,"/system/bin/su"));
 chrooted=0;strcpy(name,"/system/bin/something");assert(CALL(&p)==0);assert(!strcmp(name,"/system/bin/something"));
 strcpy(name,"/system/bin/suX");assert(CALL(&p)==0);assert(!strcmp(name,"/system/bin/suX"));
 strcpy(name,"/system/bin/su");assert(CALL(&p)==0);assert(!strcmp(name,"/system/bin/sh"));
 assert(fd==0 && mode==0 && flags==0);
 #undef CALL
}
int main(void) { check(0);check(1);return 0; }
'''
 p=t/'paths.c';p.write_text(harness)
 subprocess.run([str(cc),'-Wall','-Wextra','-Werror','-Wno-unused-parameter',str(p),'-o',str(t/'paths')],check=True)
 subprocess.run([str(t/'paths')],check=True)
 results.append({'test':'actual_hook_path_boundaries','passed':True,'cases':'both hooks: NULL pointer, NULL filename, ERR_PTR, NULL name, chroot denial, other path, prefix lookalike, exact su->sh; fd/mode/flags unchanged'})
(r/'reports/hook-contract-tests.json').write_text(json.dumps(results,indent=2)+'\n')
print('PASS:',len(results),'contract/boundary test groups; four historical mismatches rejected by compiler.')
