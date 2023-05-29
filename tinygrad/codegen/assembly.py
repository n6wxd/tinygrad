import yaml
from tinygrad.codegen.linearizer import Linearizer, UOps, LocalTypes
from tinygrad.ops import ASTRunner, BinaryOps, UnaryOps
from tinygrad.runtime.ops_gpu import ROCM_LLVM_PATH
from collections import defaultdict

# ugh, is this really needed?
from extra.helpers import enable_early_exec
early_exec = enable_early_exec()

# https://github.com/RadeonOpenCompute/ROCm_Documentation/blob/master/ROCm_Compiler_SDK/ROCm-Codeobj-format.rst
# amd_kernel_..., amd_machine_...
# kernel_code_entry_byte_offset, kernel_code_prefetch_byte_offset
# kernel_code_prefetch_byte_size, max_scratch_backing_memory_byte_size
# compute_pgm_rsrc1, compute_pgm_rsrc2, kernel_code_properties, workitem_private_segment_byte_size

# TODO: generate this struct
# amdhsa_user_sgpr_kernarg_segment_ptr
# amdhsa_system_sgpr_workgroup_id_x
# enable_sgpr_grid_workgroup_count_X
boilerplate_start = """
.global _start
_start:
.rodata
.align 0x10
.global code.kd
.type code.kd,STT_OBJECT
.amdhsa_kernel code
  .amdhsa_group_segment_fixed_size 0
  .amdhsa_private_segment_fixed_size 0
  .amdhsa_kernarg_size 0
  .amdhsa_next_free_vgpr 16  // this matters!
  .amdhsa_reserve_vcc 0
  .amdhsa_reserve_xnack_mask 0
  .amdhsa_next_free_sgpr 8
  .amdhsa_float_round_mode_32 0
  .amdhsa_float_round_mode_16_64 0
  .amdhsa_float_denorm_mode_32 3
  .amdhsa_float_denorm_mode_16_64 3
  .amdhsa_dx10_clamp 1
  .amdhsa_ieee_mode 1
  .amdhsa_fp16_overflow 0
  .amdhsa_workgroup_processor_mode 1
  .amdhsa_memory_ordered 1
  .amdhsa_forward_progress 0
  .amdhsa_enable_private_segment 0
  .amdhsa_system_sgpr_workgroup_id_x 1
  .amdhsa_system_sgpr_workgroup_id_y 0
  .amdhsa_system_sgpr_workgroup_id_z 0
  .amdhsa_system_sgpr_workgroup_info 0
  .amdhsa_system_vgpr_workitem_id 0
  .amdhsa_exception_fp_ieee_invalid_op 0
  .amdhsa_exception_fp_denorm_src 0
  .amdhsa_exception_fp_ieee_div_zero 0
  .amdhsa_exception_fp_ieee_overflow 0
  .amdhsa_exception_fp_ieee_underflow 0
  .amdhsa_exception_fp_ieee_inexact 0
  .amdhsa_exception_int_div_zero 0
  .amdhsa_user_sgpr_dispatch_ptr 0
  .amdhsa_user_sgpr_queue_ptr 0
  .amdhsa_user_sgpr_kernarg_segment_ptr 1
  .amdhsa_user_sgpr_dispatch_id 0
  .amdhsa_user_sgpr_private_segment_size 0
  .amdhsa_wavefront_size32 1
  .amdhsa_uses_dynamic_stack 0
.end_amdhsa_kernel
.text
code:
"""

# https://github.com/ROCm-Developer-Tools/ROCm-ComputeABI-Doc/blob/master/AMDGPU-ABI.md#initial-kernel-register-state
# RDNA3 is actually a SIMD machine!
# warp size of 32, s registers are shared across the warp, v are 32-wide vectors
class AssemblyCodegen(Linearizer):
  supports_float4: bool = True
  supports_float4_alu: bool = True

  # s registers are the addresses and non local indexes
  def codegen(self):
    self.process()
    self.hand_coded_optimizations()
    self.linearize()

    args = []
    for i,b in enumerate(self.bufs): args.append({'.address_space': 'global', '.name': f'buf_{i}', '.offset': i*8, '.size': 8, '.type_name': b.dtype.name+"*", '.value_kind': 'global_buffer'})

    metadata = {'amdhsa.kernels': [{'.args': args,
                  '.group_segment_fixed_size': 0, '.kernarg_segment_align': 8, '.kernarg_segment_size': len(self.bufs)*8,
                  '.language': 'OpenCL C', '.language_version': [1, 2], '.max_flat_workgroup_size': 256,
                  '.name': 'code', '.private_segment_fixed_size': 0, '.sgpr_count': 8, '.sgpr_spill_count': 0,
                  '.symbol': 'code.kd', '.uses_dynamic_stack': False, '.vgpr_count': 256, '.vgpr_spill_count': 0,
                  '.wavefront_size': 32}],
                'amdhsa.target': 'amdgcn-amd-amdhsa--gfx1100', 'amdhsa.version': [1, 2]}

    local_size = [128]
    #local_size = [32]
    #local_size = [1]

    ins = []

    # add work group x before we smash s2
    #ins.append('s_load_b32 s3, s[0:1], 0x24')
    #ins.append('s_waitcnt lgkmcnt(0)')
    #ins.append(f's_mov_b32 s3, {local_size[0]}')  # local size
    ins.append(f's_mul_i32 s3, s2, {local_size[0]}')  # TODO: how do i get this dynamicly?
    ins.append('v_add_co_u32 v0, vcc_lo, s3, v0')

    # TODO: combine the loads
    pend_i = []
    for i in list(range(len(self.bufs)))[::-1]:
      ins.append(f's_load_b64 s[{i*2}:{i*2+1}], s[0:1], {i*8}')
      pend_i.append(f"s[{i*2}:{i*2+1}]")

    # v0 is a float offset
    # TODO: compute indexes
    #ins.append('v_lshlrev_b32 v0, 2, v0')
    ins.append('v_lshlrev_b32 v0, 4, v0')

    name_to_v = {}
    latest_v = 1
    ready = defaultdict(lambda: False)
    pend_v = []
    def get_i(i):
      nonlocal latest_v, name_to_v, pend_v, pend_i
      ret = f"s[{i*2}:{i*2+1}]"
      if not ready[ret]:
        ins.append('s_waitcnt lgkmcnt(0)')
        for x in pend_i: ready[x] = True
        pend_i = []
      return ret

    # TODO: free vs that aren't used again with liveness analysis
    def get_v(var, needs_wait=False):
      nonlocal latest_v, name_to_v, pend_v, pend_i
      if var.name not in name_to_v:
        sz = 4 if '4' in var.ltype.name else 1 # HACK
        #if latest_v%sz != 0: latest_v += sz - latest_v%sz  # alignment
        name_to_v[var.name] = f"v{latest_v}" if sz == 1 else f"v[{latest_v}:{latest_v+sz-1}]"
        if needs_wait:
          pend_v.append(name_to_v[var.name])
        else:
          ready[name_to_v[var.name]] = True
        latest_v += sz
      else:
        if not ready[name_to_v[var.name]]:
          ins.append('s_waitcnt vmcnt(0)')
          for x in pend_v: ready[x] = True
          pend_v = []
      if var.offset != None:
        # ugh
        vv = int(name_to_v[var.name].split("[")[1].split(":")[0]) + var.offset
        return f"v{vv}"
      else:
        return name_to_v[var.name]

    global_size = []
    for uop,newvar,vin,args in self.uops:
      if uop == UOps.LOOP:
        if args[1] == "global":
          for i,var in enumerate(args[0]):
            global_size.append(var.max+1)
      elif uop == UOps.LOAD:
        # TODO: indexing and valid
        ins.append(f'global_load_{"b128" if "4" in newvar.ltype.name else "b32"} {get_v(newvar, True)}, v0, {get_i(args.i)}')
      elif uop == UOps.ALU:
        if args == BinaryOps.ADD:
          if newvar.ltype == LocalTypes.float4:
            v1, v2, v3 = get_v(newvar), get_v(vin[0]), get_v(vin[1])
            v1, v2, v3 = [int(x.split("[")[1].split(":")[0]) for x in [v1, v2, v3]]
            for off in range(0,4,2): ins.append(f'v_dual_add_f32 v{v1+off+0}, v{v2+off+0}, v{v3+off+0} :: v_dual_add_f32 v{v1+off+1}, v{v2+off+1}, v{v3+off+1}')
          else:
            ins.append(f'v_add_f32_e32 {get_v(newvar)}, {get_v(vin[0])}, {get_v(vin[1])}')
          #ins.append('s_delay_alu instid0(VALU_DEP_1)')
        elif args == BinaryOps.SUB:
          ins.append(f'v_sub_f32_e32 {get_v(newvar)}, {get_v(vin[0])}, {get_v(vin[1])}')
        elif args == BinaryOps.MUL:
          if newvar.ltype == LocalTypes.float4:
            v1, v2, v3 = get_v(newvar), get_v(vin[0]), get_v(vin[1])
            v1, v2, v3 = [int(x.split("[")[1].split(":")[0]) for x in [v1, v2, v3]]
            for off in range(0,4,2): ins.append(f'v_dual_mul_f32 v{v1+off+0}, v{v2+off+0}, v{v3+off+0} :: v_dual_mul_f32 v{v1+off+1}, v{v2+off+1}, v{v3+off+1}')
          else:
            ins.append(f'v_mul_f32_e32 {get_v(newvar)}, {get_v(vin[0])}, {get_v(vin[1])}')
        elif args == UnaryOps.LOG:
          nv = get_v(newvar)
          ins.append(f'v_log_f32_e32 {nv}, {get_v(vin[0])}')      # this is log base 2!
          ins.append(f'v_mul_f32_e32 {nv}, 0.69314718056, {nv}')  # log(2)/log(e)
        else:
          raise NotImplementedError(f"missing imp for ALU op {args}")
      elif uop == UOps.STORE:
        ins.append(f'global_store_{"b128" if "4" in vin[0].ltype.name else "b32"} v0, {get_v(vin[0])}, {get_i(args.i)}')

      #print(uop)

    # move to vector reg
    #ins.append('v_add_co_ci_u32_e32 v1, vcc_lo, s1, v1, vcc_lo')
    #ins.append('v_add_co_ci_u32_e32 v0, vcc_lo, s0, v0, vcc_lo')

    """
    # store. NOTE: v0 contains offset at launch
    #ins.append('v_dual_mov_b32 v0, 0 :: v_dual_mov_b32 v1, 2.0')
    #ins.append('v_mov_b32 v0, 4')
    ins.append('v_lshlrev_b32 v0, 2, v0')
    ins.append('v_mov_b32 v1, 2.0')
    ins.append('global_store_b32 v0, v1, s[0:1]')
    #ins.append('global_store_b32 v0, v1, s[2:3]')
    #ins.append('global_store_b32 v0, v1, s[4:5]')
    """

    # exit asm
    ins += ['s_sendmsg sendmsg(MSG_DEALLOC_VGPRS)', 's_endpgm', 's_code_end']

    code = boilerplate_start + '\n'.join(ins) + "\n.amdgpu_metadata\n" + yaml.dump(metadata) + ".end_amdgpu_metadata"
    object = early_exec(([ROCM_LLVM_PATH / "llvm-mc", '--arch=amdgcn', '--mcpu=gfx1100', '--triple=amdgcn-amd-amdhsa', '--filetype=obj', '-'], code.encode("utf-8")))
    asm = early_exec(([ROCM_LLVM_PATH / "ld.lld", "/dev/stdin", "-o", "/dev/stdout", "--pie"], object))

    #from hexdump import hexdump
    #hexdump(asm)
    #global_size = [7]

    return ASTRunner('code', asm,
      global_size[::-1] if len(global_size) else [1], local_size[::-1] if len(local_size) else None,
      op_estimate=self.info.flops, mem_estimate=self.mem_estimate, display_name=self.display_name, runtime_args={"binary": True})
