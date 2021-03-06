# GPU accelerated single precision matrix multiplication (single thread)
import numpy as np
import struct
import sys
import time
import random
# from PIL import Image

from videocore.assembler import qpu, assemble, print_qbin
from videocore.driver import Driver

def mask(*idxs):
    values = [1]*16
    for idx in idxs:
        values[idx] = 0
    return values

@qpu
def sgemm_gpu_code(asm):
    NCOLS_IDXS = [0]*4
    LOAD_SETUP_IDXS = [0]*4
    STORE_SETUP_IDXS = [0]*4

    B_CUR_IDX = 0;       LOAD_BLOCKS_IDX = 0
    K_IDX = 1;           STORE_BLOCKS_IDX = 1
    I_IDX = 2;           NROWS_IDX = 2
    J_IDX = 3;           NCOLS_IDXS[0] = 3
    P_IDX = 4;           NCOLS_IDXS[1] = 4
    Q_IDX = 5;           NCOLS_IDXS[2] = 5
    R_IDX = 6;           NCOLS_IDXS[3] = 6
    A_CUR_IDX = 7;       LOAD_SETUP_IDXS[0] = 7;  ROW_IDX = 7
    C_CUR_IDX = 8;       LOAD_SETUP_IDXS[1] = 8
    A_BASE_IDX = 9;      LOAD_SETUP_IDXS[2] = 9
    B_BASE_IDX = 10;     LOAD_SETUP_IDXS[3] = 10
    C_BASE_IDX = 11;     STORE_SETUP_IDXS[0] = 11
    A_STRIDE_IDX = 12;   STORE_SETUP_IDXS[1] = 12
    B_STRIDE_IDX = 13;   STORE_SETUP_IDXS[2] = 13
    C_STRIDE_IDX = 14;   STORE_SETUP_IDXS[3] = 14
    COEF_ADDR_IDX = 15

    # Semaphore
    COMPLETED = 0

    ra = [ ra0 , ra1 , ra2 , ra3 , ra4 , ra5 , ra6 , ra7,
           ra8 , ra9 , ra10, ra11, ra12, ra13, ra14, ra15,
           ra16, ra17, ra18, ra19, ra20, ra21, ra22, ra23,
           ra24, ra25, ra26, ra27, ra28, ra29, ra30, ra31 ]
    rb = [ rb0 , rb1 , rb2 , rb3 , rb4 , rb5 , rb6 , rb7,
           rb8 , rb9 , rb10, rb11, rb12, rb13, rb14, rb15,
           rb16, rb17, rb18, rb19, rb20, rb21, rb22, rb23,
           rb24, rb25, rb26, rb27, rb28, rb29, rb30, rb31 ]

    #==== Load constants ====
    # Load constants to r2.
    mov(r0, uniform)    # uniforms address
    mov(r2, 1)
    ldi(null, mask(P_IDX), set_flags=True)
    mov(r2, uniform, cond='zs')     # p
    ldi(null, mask(Q_IDX), set_flags=True)
    mov(r2, uniform, cond='zs')     # q
    ldi(null, mask(R_IDX), set_flags=True)
    mov(r2, uniform, cond='zs')     # r
    ldi(null, mask(A_BASE_IDX), set_flags=True)
    mov(r2, uniform, cond='zs')     # Address of A[0,0]
    ldi(null, mask(B_BASE_IDX), set_flags=True)
    mov(r2, uniform, cond='zs')     # Address of B[0,0]
    ldi(null, mask(C_BASE_IDX), set_flags=True)
    mov(r2, uniform, cond='zs')     # Address of C[0,0]
    ldi(null, mask(A_STRIDE_IDX), set_flags=True)
    mov(r2, uniform, cond='zs')     # A stride
    ldi(null, mask(B_STRIDE_IDX), set_flags=True)
    mov(r2, uniform, cond='zs')     # B stride
    ldi(null, mask(C_STRIDE_IDX), set_flags=True)
    mov(r2, uniform, cond='zs')     # C stride
    ldi(null, mask(COEF_ADDR_IDX), set_flags=True)
    ldi(r1, 4*10)
    iadd(r2, r0, r1, cond='zs')     # address of alpha and beta

    #==== Variables ====

    # A_base = address of A[0,0] + (p+15)/16*16*A_stride
    # B_base = address of B[0,0]                         + (r+31)/32*32*B_stride
    # C_base = address of C[0,0] + (p+15)/16*16*C_stride + (r+31)/32*32*4

    # A_cur = A_base - i*16*A_stride
    # B_cur = B_base                 - j*32*b_stride
    # C_cur = C_base - i*16*C_stride - j*32*4

    rotate(broadcast, r2, -P_IDX)
    iadd(r0, r5, 15)
    shr(r0, r0, 4)
    shl(r0, r0, 4)                  # r0=(p+15)/16*16
    rotate(broadcast, r2, -R_IDX)
    ldi(r1, 31)
    iadd(r1, r1, r5)
    shr(r1, r1, 5)
    shl(r1, r1, 5)                  # r1=(r+31)/32*32
    rotate(broadcast, r2, -A_STRIDE_IDX)
    imul24(r3, r5, r0)              # r3=(p+15)/16*16*A_stride
    ldi(null, mask(A_BASE_IDX), set_flags=True)
    iadd(r2, r2, r3, cond='zs')
    rotate(broadcast, r2, -B_STRIDE_IDX)
    imul24(r3, r1, r5)              # r3=(r+31)/32*32*B_stride
    ldi(null, mask(B_BASE_IDX), set_flags=True)
    iadd(r2, r2, r3, cond='zs')
    rotate(broadcast, r2, -C_STRIDE_IDX)
    shl(r1, r1, 2)                  # r1=(r+31)/32*32*4
    imul24(r3, r5, r0)              # r3=(p+15)/16*16*C_stride
    ldi(null, mask(C_BASE_IDX), set_flags=True)
    iadd(r2, r2, r3, cond='zs', set_flags=False)
    iadd(r2, r2, r1, cond='zs')

    # Disable swapping of two TMUs.
    mov(tmu_noswap, 1)

    # Initialize column vectors.
    for i in range(32):
        mov(ra[i],  0.0).mov(rb[i],  0.0)

    #==== i-loop ====

    # Initialize i.
    # i=(p+15)/16.
    rotate(broadcast, r2, -P_IDX)
    iadd(r0, r5, 15)
    ldi(null, mask(I_IDX), set_flags=True)
    shr(r2, r0, 4, cond='zs')

    L.i_loop

    #==== j-loop ====

    # Initialize j.
    # j=(r+31)/32.
    rotate(broadcast, r2, -R_IDX)
    ldi(r0, 31)
    iadd(r0, r0, r5)
    ldi(null, mask(J_IDX), set_flags=True)
    shr(r2, r0, 5, cond='zs')

    rotate(broadcast, r2, -I_IDX)
    shl(r0, r5, 4)                          # r0=16*i
    rotate(broadcast, r2, -A_STRIDE_IDX)
    imul24(r0, r0, r5)                      # r0=16*i*A_stride
    rotate(broadcast, r2, -A_BASE_IDX)
    ldi(null, mask(A_CUR_IDX), set_flags=True)
    isub(r2, r5, r0, cond='zs')

    L.j_loop

    rotate(broadcast, r2, -I_IDX)
    shl(r0, r5, 4)                          # r0=16*i
    rotate(broadcast, r2, -C_STRIDE_IDX)
    imul24(r0, r0, r5)                      # r0=16*i*C_stride
    rotate(broadcast, r2, -J_IDX)
    shl(r1, r5, 7)                          # r1=4*32*j
    rotate(broadcast, r2, -C_BASE_IDX)
    ldi(null, mask(C_CUR_IDX), set_flags=True)
    isub(r2, r5, r0, cond='zs', set_flags=False)
    isub(r2, r2, r1, cond='zs')

    rotate(broadcast, r2, -J_IDX)
    shl(r0, r5, 5)                          # r0=32*j
    rotate(broadcast, r2, -B_STRIDE_IDX)
    imul24(r0, r0, r5)                      # r0=32*j*B_stride
    rotate(broadcast, r2, -B_BASE_IDX)
    ldi(null, mask(B_CUR_IDX), set_flags=True)
    isub(r2, r5, r0, cond='zs')

    #==== k-loop ====
    # r2[1] = q (k=q)
    nop()
    rotate(broadcast, r2, -Q_IDX)
    ldi(null, mask(K_IDX), set_flags=True)
    mov(r2, r5, cond='zs')

    rotate(broadcast, r2, -A_STRIDE_IDX)
    imul24(r0, element_number, r5)
    rotate(broadcast, r2, -A_CUR_IDX)
    iadd(r0, r0, r5)
    mov(tmu0_s, r0) # r1[e] = A_cur + A_stride*e + (q-k)*4

    L.k_loop

    shl(r0, element_number, 2)               # r0[e] = e*4
    rotate(broadcast, r2, -B_STRIDE_IDX)
    mov(r1, r5)                              # r1 = B_stride
    rotate(broadcast, r2, -B_CUR_IDX)
    iadd(r0, r0, r5)                         # r0[e] = B_cur + e*4
    mov(tmu1_s, r0)                          # tmu1[e] = B_cur + e*4 + B_stride*0
    iadd(r0, r0, r1)
    mov(tmu1_s, r0)                          # tmu1[e] = B_cur + e*4 + B_stride*1
    iadd(r0, r0, r1)
    mov(tmu1_s, r0)                          # tmu1[e] = B_cur + e*4 + B_stride*2
    iadd(r0, r0, r1)
    mov(tmu1_s, r0)                          # tmu1[e] = B_cur + e*4 + B_stride*3

    nop(sig='load tmu0')
    mov(r3, r4)
    rotate(broadcast, r2, -A_STRIDE_IDX)
    imul24(r0, element_number, r5)
    rotate(broadcast, r2, -A_CUR_IDX)
    iadd(r1, r0, r5)
    rotate(broadcast, r2, -Q_IDX)
    mov(r0, r5)
    rotate(broadcast, r2, -K_IDX)
    isub(r0, r0, r5)
    iadd(r0, r0, 1)
    shl(r0, r0, 2)
    iadd(tmu0_s, r1, r0)

    bxor(rb[16], r0, r0, sig='load tmu1')    # load TMU sig for line 0
    rotate(broadcast, r2, -K_IDX)
    isub(null, element_number, r5, set_flags=True)
    mov(rb[16], r4, cond='ns')
    mov(broadcast, r4)
    fmul(r0, r3, r5)
    fadd(rb[0], rb[0], r0)
    bxor(ra[16], r0, r0, sig='load tmu1')    # load TMU sig for line 0
    rotate(broadcast, r2, -K_IDX)
    isub(null, element_number, r5, set_flags=True)
    mov(ra[16], r4, cond='ns')
    mov(broadcast, r4)
    fmul(r0, r3, r5)
    fadd(ra[0], ra[0], r0)
    bxor(rb[17], r0, r0, sig='load tmu1')    # load TMU sig for line 0
    rotate(broadcast, r2, -K_IDX)
    isub(null, element_number, r5, set_flags=True)
    mov(rb[17], r4, cond='ns')
    mov(broadcast, r4)
    fmul(r0, r3, r5)
    fadd(rb[1], rb[1], r0)
    bxor(ra[17], r0, r0, sig='load tmu1')    # load TMU sig for line 0
    rotate(broadcast, r2, -K_IDX)
    isub(null, element_number, r5, set_flags=True)
    mov(ra[17], r4, cond='ns')
    mov(broadcast, r4)
    fmul(r0, r3, r5)
    fadd(ra[1], ra[1], r0)

    # load TMU line
    for l in range(1, 8):
        shl(r0, element_number, 2)               # r0[e] = e*4
        rotate(broadcast, r2, -B_STRIDE_IDX)
        mov(r1, r5)                              # r1 = B_stride
        rotate(broadcast, r2, -B_CUR_IDX)
        iadd(r0, r0, r5)                         # r0[e] = B_cur + e*4
        for i in range(l*4):
            iadd(r0, r0, r1)
        mov(tmu1_s, r0)                          # tmu1[e] = B_cur + e*4 + B_stride*0
        iadd(r0, r0, r1)
        mov(tmu1_s, r0)                          # tmu1[e] = B_cur + e*4 + B_stride*1
        iadd(r0, r0, r1)
        mov(tmu1_s, r0)                          # tmu1[e] = B_cur + e*4 + B_stride*2
        iadd(r0, r0, r1)
        mov(tmu1_s, r0)                          # tmu1[e] = B_cur + e*4 + B_stride*3

        bxor(rb[16+2*l], r0, r0, sig='load tmu1')    # load TMU sig for line 0
        rotate(broadcast, r2, -K_IDX)
        isub(null, element_number, r5, set_flags=True)
        mov(rb[16+2*l], r4, cond='ns')
        mov(broadcast, r4)
        fmul(r0, r3, r5)
        fadd(rb[2*l], rb[2*l], r0)
        bxor(ra[16+2*l], r0, r0, sig='load tmu1')    # load TMU sig for line 1
        rotate(broadcast, r2, -K_IDX)
        isub(null, element_number, r5, set_flags=True)
        mov(ra[16+2*l], r4, cond='ns')
        mov(broadcast, r4)
        fmul(r0, r3, r5)
        fadd(ra[2*l], ra[2*l], r0)
        bxor(rb[16+2*l+1], r0, r0, sig='load tmu1')  # load TMU sig for line 2
        rotate(broadcast, r2, -K_IDX)
        isub(null, element_number, r5, set_flags=True)
        mov(rb[16+2*l+1], r4, cond='ns')
        mov(broadcast, r4)
        fmul(r0, r3, r5)
        fadd(rb[2*l+1], rb[2*l+1], r0)
        bxor(ra[16+2*l+1], r0, r0, sig='load tmu1')  # load TMU sig for line 3
        rotate(broadcast, r2, -K_IDX)
        isub(null, element_number, r5, set_flags=True)
        mov(ra[16+2*l+1], r4, cond='ns')
        mov(broadcast, r4)
        fmul(r0, r3, r5)
        fadd(ra[2*l+1], ra[2*l+1], r0)

    for i in range(1, 16):
        nop(sig='load tmu0')
        mov(r3, r4)
        rotate(broadcast, r2, -A_STRIDE_IDX)
        imul24(r0, element_number, r5)
        rotate(broadcast, r2, -A_CUR_IDX)
        iadd(r1, r0, r5)
        rotate(broadcast, r2, -Q_IDX)
        mov(r0, r5)
        rotate(broadcast, r2, -K_IDX)
        isub(r0, r0, r5)
        iadd(r0, r0, 1)
        iadd(r0, r0, i)
        shl(r0, r0, 2)
        iadd(tmu0_s, r1, r0)

        mov(r1, rb[16])
        nop()
        rotate(broadcast, r1, -i)
        mov(r1, ra[16])         .fmul(r0, r3, r5)
        for j in range(0, 15):
            fadd(rb[j],  rb[j],  r0)
            rotate(broadcast, r1, -i)
            mov(r1, rb[17+j])         .fmul(r0, r3, r5)
            fadd(ra[j],  ra[j],  r0)
            rotate(broadcast, r1, -i)
            mov(r1, ra[17+j])         .fmul(r0, r3, r5)
        fadd(rb[15],  rb[15],  r0)
        rotate(broadcast, r1, -i)
        fmul(r0, r3, r5)
        fadd(ra[15],  ra[15],  r0)

    ldi(r1, 64)
    ldi(null, mask(B_CUR_IDX), set_flags=True)
    iadd(r2, r2, r1, cond='zs')

    ldi(r1, 16)
    rotate(broadcast, r2, -K_IDX)
    isub(r1, r5, r1)
    imax(r1, r1, 0)

    jzc(L.k_loop)
    ldi(null, mask(K_IDX), set_flags=True) # delay slot
    mov(r2, r1, cond='zs')                 # delay slot
    nop()                                  # delay slot

    #==== end of k-loop ====

    nop(sig='load tmu0')

    rotate(broadcast, r2, -C_STRIDE_IDX)
    ldi(r0, 8192)
    isub(r0, r0, r5, set_flags=True)
    jns(L.dma_for_large_stride)
    nop() # delay slot
    nop() # delay slot
    nop() # delay slot
    if True:

        # nrows = min(P-((P+15)/16-i)*16, 16)
        rotate(broadcast, r2, -P_IDX)
        iadd(r1, r5, 15)
        shr(r1, r1, 4)
        rotate(broadcast, r2, -I_IDX)
        isub(r1, r1, r5)
        shl(r1, r1, 4)
        rotate(broadcast, r2, -P_IDX)
        isub(r1, r5, r1)
        rotate(broadcast, r1, 0)
        ldi(r1, 16)
        imin(r1, r1, r5)
        ldi(null, mask(NROWS_IDX), set_flags=True)
        mov(r3, r1, cond='zs')

        for block in range(2):
            # ncols = min(R-(((R+31)/32-j)*2+block)*16, 16)
            ldi(r1, 31)
            rotate(broadcast, r2, -R_IDX)
            iadd(r1, r1, r5)
            shr(r1, r1, 5)
            rotate(broadcast, r2, -J_IDX)
            isub(r1, r1, r5)
            shl(r1, r1, 1)
            iadd(r1, r1, block)
            shl(r1, r1, 4)
            rotate(broadcast, r2, -R_IDX)
            isub(r1, r5, r1)
            rotate(broadcast, r1, 0)
            ldi(r1, 16)
            imin(r1, r1, r5)
            # band(r1, r1, 0xF)
            ldi(null, mask(NCOLS_IDXS[block]), set_flags=True)
            mov(r3, r1, cond='zs')

            # load setup params
            band(r1, r1, 0xF)
            shl(r1, r1, 4)     # ncols<<4
            rotate(broadcast, r3, -NROWS_IDX)
            band(r0, r5, 0xF)
            bor(r1, r1, r0)    # ncols<<4|nrows
            shl(r1, r1, 8)     # (ncols<<4|nrows)<<8
            shl(r1, r1, 8)     # (ncols<<4|nrows)<<8<<8 = ncols<<20|nrows<<16
            ldi(r0,
                0x80000000|    # setup_dma_load
                0<<28|         # 32bit
                0<<24|         # use the extended pitch setup register
                1<<12|         # vpitch=1
                0<<11|         # horizontal
                (block*16)<<4| # Y=16*block
                0)             # X=0
            ldi(null, mask(LOAD_SETUP_IDXS[block]), set_flags=True)
            bor(r3, r0, r1, cond='zs')

            # store setup params
            rotate(broadcast, r3, -NROWS_IDX)
            shl(r1, r5, 7)     # nrows<<7
            rotate(broadcast, r3, -NCOLS_IDXS[block])
            bor(r1, r1, r5)    # nrows<<7|ncols
            shl(r1, r1, 8)     # (nrows<<7|ncols)<<8
            shl(r1, r1, 8)     # (nrows<<7|ncols)<<8<<8 = nrows<<23|ncols<<16
            ldi(r0,
                0x80000000|    # setup_dma_store
                1<<14|         # horizontal
                (16*block)<<7| # Y=16*block
                0<<3|          # X=0
                0)             # 32bit
            ldi(null, mask(STORE_SETUP_IDXS[block]), set_flags=True)
            bor(r3, r0, r1, cond='zs')

        def setup_dma_load_block(block):
            rotate(broadcast, r3, -LOAD_SETUP_IDXS[block]) # will be delay slot
            nop()                                          # will be delay slot
            mov(vpmvcd_rd_setup, r5)

        def setup_dma_store_block(block):
            # stride = C_stride - 4 * ncols
            rotate(broadcast, r3, -NCOLS_IDXS[block]) # will be delay slot
            imul24(r1, r5, 4)                         # will be delay slot
            rotate(broadcast, r2, -C_STRIDE_IDX)
            isub(r1, r5, r1)
            setup_dma_store_stride(r1)

            rotate(broadcast, r3, -STORE_SETUP_IDXS[block])
            mov(vpmvcd_wr_setup, r5)

        # blocks = min((R-((R+31)/32-j)*32+15)/16, 2)
        ldi(r1, 31)                   # 31
        rotate(broadcast, r2, -R_IDX)
        iadd(r1, r1, r5)              # R+31
        shr(r1, r1, 5)                # (R+31)/32
        rotate(broadcast, r2, -J_IDX)
        isub(r1, r1, r5)              # (R+31)/32-j
        shl(r1, r1, 5)                # ((R+31)/32-j)*32
        rotate(broadcast, r2, -R_IDX)
        isub(r1, r5, r1)              # R-((R+31)/32-j)*32
        iadd(r1, r1, 15)              # R-((R+31)/32-j)*32+15
        shr(r1, r1, 4)                # (R-((R+31)/32-j)*32+15)/16
        imin(r1, r1, 2)
        ldi(null, mask(LOAD_BLOCKS_IDX, STORE_BLOCKS_IDX), set_flags=True)
        mov(r3, r1, cond='zs')

        mutex_acquire()

        # Set stride for DMA to load and store C.
        rotate(broadcast, r2, -C_STRIDE_IDX)
        setup_dma_load_stride(r5, tmp_reg=r1)

        # Issue load of block 0
        setup_dma_load_block(0)
        rotate(broadcast, r2, -C_CUR_IDX)
        start_dma_load(r5)
        rotate(broadcast, r3, -LOAD_BLOCKS_IDX)
        ldi(null, mask(LOAD_BLOCKS_IDX), set_flags=True)
        isub(r3, r5, 1, cond='zs')

        # Issue loading of block 1
        rotate(broadcast, r3, -LOAD_BLOCKS_IDX)
        mov(r0, r5, set_flags=True)
        jzs(L.skip_load_block_1)
        wait_dma_load()  # Wait for load of block 0  # delay slot
        setup_dma_load_block(1)                      # delay slot (head 2 instruction)
        ldi(r0, 4*16)
        rotate(broadcast, r2, -C_CUR_IDX)
        iadd(vpm_ld_addr, r5, r0)
        ldi(null, mask(LOAD_BLOCKS_IDX), set_flags=True)
        isub(r3, r3, 1, cond='zs')
        L.skip_load_block_1

        # Load alpha and beta.
        rotate(r0, r2, -COEF_ADDR_IDX)
        mov(uniforms_address, r0)

        # Setup VPM access for block 0
        setup_vpm_read(mode='32bit vertical', Y=0, X=0, nrows=16)
        setup_vpm_write(mode='32bit vertical', Y=0, X=0)

        mov(r1, uniform)        # r1=alpha
        mov(broadcast, uniform) # r5=beta

        fmul(rb0, rb0, r1)
        fmul(r0, vpm, r5)
        for i in range(7):
            fadd(vpm, rb[i], r0).fmul(ra[i], ra[i], r1)
            mov(rb[i], 0.0)     .fmul(r0, vpm, r5)
            fadd(vpm, ra[i], r0).fmul(rb[i+1], rb[i+1], r1)
            mov(ra[i], 0.0)     .fmul(r0, vpm, r5)
        fadd(vpm, rb7, r0).fmul(ra7, ra7, r1)
        mov(rb7, 0.0)     .fmul(r0, vpm, r5)
        fadd(vpm, ra7, r0)
        mov(ra7, 0.0)

        # Issue store of block 0
        setup_dma_store_block(0)
        rotate(broadcast, r2, -C_CUR_IDX)
        start_dma_store(r5)
        ldi(null, mask(STORE_BLOCKS_IDX), set_flags=True)
        isub(r3, r3, 1, cond='zs')

        wait_dma_load()  # Wait for load of block 1

        # Load alpha and beta.
        rotate(r0, r2, -COEF_ADDR_IDX)
        mov(uniforms_address, r0)

        # Setup VPM access for block 1
        setup_vpm_read(mode='32bit vertical', Y=16, X=0, nrows=16)
        setup_vpm_write(mode='32bit vertical', Y=16, X=0)

        mov(r1, uniform)        # r1=alpha
        mov(broadcast, uniform) # r5=beta

        fmul(rb8, rb8, r1)
        fmul(r0, vpm, r5)
        for i in range(8, 15):
            fadd(vpm, rb[i], r0).fmul(ra[i], ra[i], r1)
            mov(rb[i], 0.0)     .fmul(r0, vpm, r5)
            fadd(vpm, ra[i], r0).fmul(rb[i+1], rb[i+1], r1)
            mov(ra[i], 0.0)     .fmul(r0, vpm, r5)
        fadd(vpm, rb15, r0).fmul(ra15, ra15, r1)
        mov(rb15, 0.0)     .fmul(r0, vpm, r5)
        fadd(vpm, ra15, r0)
        mov(ra15, 0.0)

        # Issue store of block 1
        rotate(broadcast, r3, -STORE_BLOCKS_IDX)
        mov(r0, r5, set_flags=True)
        jzs(L.skip_store_block_1)
        wait_dma_store() # Wait for store of block 0  # delay slot
        setup_dma_store_block(1)                      # delay slot (head 2 instruction)
        ldi(r0, 4*16)
        rotate(broadcast, r2, -C_CUR_IDX)
        iadd(vpm_st_addr, r5, r0)
        ldi(null, mask(STORE_BLOCKS_IDX), set_flags=True)
        isub(r3, r3, 1, cond='zs')
        L.skip_store_block_1

        wait_dma_store() # Wait for store of block 1
        mutex_release()

        jmp(L.dma_done)
        nop() # delay slot
        nop() # delay slot
        nop() # delay slot

    L.dma_for_large_stride
    if True:
        mov(r3, 1)

        # nrows = min(P-((P+15)/16-i)*16, 16)
        rotate(broadcast, r2, -P_IDX)
        iadd(r1, r5, 15)
        shr(r1, r1, 4)
        rotate(broadcast, r2, -I_IDX)
        isub(r1, r1, r5)
        shl(r1, r1, 4)
        rotate(broadcast, r2, -P_IDX)
        isub(r1, r5, r1)
        rotate(broadcast, r1, 0)
        ldi(r1, 16)
        imin(r1, r1, r5)
        ldi(null, mask(NROWS_IDX), set_flags=True)
        mov(r3, r1, cond='zs')

        for block in range(2):
            # ncols = min(R-(((R+31)/32-j)*2+block)*16, 16)
            ldi(r1, 31)
            rotate(broadcast, r2, -R_IDX)
            iadd(r1, r1, r5)
            shr(r1, r1, 5)
            rotate(broadcast, r2, -J_IDX)
            isub(r1, r1, r5)
            shl(r1, r1, 1)
            iadd(r1, r1, block)
            shl(r1, r1, 4)
            rotate(broadcast, r2, -R_IDX)
            isub(r1, r5, r1)
            rotate(broadcast, r1, 0)
            ldi(r1, 16)
            imin(r1, r1, r5)
            # band(r1, r1, 0xF)
            ldi(null, mask(NCOLS_IDXS[block]), set_flags=True)
            mov(r3, r1, cond='zs')

        # blocks = min((R-((R+31)/32-j)*32+15)/16, 4)
        ldi(r1, 31)                   # 31
        rotate(broadcast, r2, -R_IDX)
        iadd(r1, r1, r5)              # R+31
        shr(r1, r1, 5)                # (R+31)/32
        rotate(broadcast, r2, -J_IDX)
        isub(r1, r1, r5)              # (R+31)/32-j
        shl(r1, r1, 5)                # ((R+31)/32-j)*32
        rotate(broadcast, r2, -R_IDX)
        isub(r1, r5, r1)              # R-((R+31)/32-j)*32
        iadd(r1, r1, 15)              # R-((R+31)/32-j)*32+15
        shr(r1, r1, 4)                # (R-((R+31)/32-j)*32+15)/16
        imin(r1, r1, 4)
        ldi(null, mask(LOAD_BLOCKS_IDX, STORE_BLOCKS_IDX), set_flags=True)
        mov(r3, r1, cond='zs')

        mutex_acquire()

        # Issue load of block 0
        ldi(null, mask(LOAD_BLOCKS_IDX), set_flags=True)
        isub(r3, r3, 1, cond='zs')
        nop()

        rotate(broadcast, r3, -NROWS_IDX)
        ldi(null, mask(ROW_IDX), set_flags=True)
        mov(r3, r5, cond='zs')
        nop()
        L.dma_for_large_stride_row_load_block_0_loop
        if True:
            rotate(broadcast, r3, -NCOLS_IDXS[0])
            band(r1, r5, 0xF)
            shl(r1, r1, 4)     # ncols<<4
            bor(r1, r1, 1)     # ncols<<4|1
            shl(r1, r1, 8)     # (ncols<<4|1)<<8
            shl(r1, r1, 8)     # (ncols<<4|1)<<8<<8 = ncols<<20|1<<16
            ldi(r0,
                0x80000000|    # setup_dma_load
                0<<28|         # 32bit
                0<<24|         # use the extended pitch setup register
                1<<12|         # vpitch=1
                0<<11|         # horizontal
                (0*16)<<4|     # Y=16*0
                0)             # X=0
            bor(r0, r0, r1)
            rotate(broadcast, r3, -NROWS_IDX)
            mov(r1, r5)
            rotate(broadcast, r3, -ROW_IDX)
            isub(r1, r1, r5)
            shl(r1, r1, 4)
            bor(vpmvcd_rd_setup, r0, r1)
            rotate(broadcast, r3, -NROWS_IDX)
            mov(r1, r5)
            rotate(broadcast, r3, -ROW_IDX)
            isub(r1, r1, r5)
            rotate(broadcast, r2, -C_CUR_IDX)
            mov(r0, r5)
            rotate(broadcast, r2, -C_STRIDE_IDX)
            imul24(r1, r1, r5)
            iadd(r0, r0, r1)
            wait_dma_load()
            start_dma_load(r0)

        ldi(null, mask(ROW_IDX), set_flags=True)
        isub(r3, r3, 1, cond='zs')
        jzc(L.dma_for_large_stride_row_load_block_0_loop)
        nop() # delay slot
        nop() # delay slot
        nop() # delay slot
        wait_dma_load()

        # Load alpha and beta.
        rotate(r0, r2, -COEF_ADDR_IDX)
        mov(uniforms_address, r0)

        # Setup VPM access for block 0
        setup_vpm_read(mode='32bit vertical', Y=0, X=0, nrows=16)
        setup_vpm_write(mode='32bit vertical', Y=0, X=0)

        mov(r1, uniform)        # r1=alpha
        mov(broadcast, uniform) # r5=beta

        fmul(rb0, rb0, r1)
        fmul(r0, vpm, r5)
        for i in range(7):
            fadd(vpm, rb[i], r0).fmul(ra[i], ra[i], r1)
            mov(rb[i], 0.0)     .fmul(r0, vpm, r5)
            fadd(vpm, ra[i], r0).fmul(rb[i+1], rb[i+1], r1)
            mov(ra[i], 0.0)     .fmul(r0, vpm, r5)
        fadd(vpm, rb7, r0).fmul(ra7, ra7, r1)
        mov(rb7, 0.0)     .fmul(r0, vpm, r5)
        fadd(vpm, ra7, r0)
        mov(ra7, 0.0)

        # Issue store of block 0
        ldi(null, mask(STORE_BLOCKS_IDX), set_flags=True)
        isub(r3, r3, 1, cond='zs')
        nop()

        rotate(broadcast, r3, -NROWS_IDX)
        ldi(null, mask(ROW_IDX), set_flags=True)
        mov(r3, r5, cond='zs')
        nop()
        L.dma_for_large_stride_row_store_block_0_loop
        if True:
            # store setup params
            mov(r1, 1)
            shl(r1, r1, 7)     # 1<<7
            rotate(broadcast, r3, -NCOLS_IDXS[0])
            bor(r1, r1, r5)    # 1<<7|ncols
            shl(r1, r1, 8)     # (1<<7|ncols)<<8
            shl(r1, r1, 8)     # (1<<7|ncols)<<8<<8 = 1<<23|ncols<<16
            ldi(r0,
                0x80000000|    # setup_dma_store
                1<<14|         # vertical
                (16*0)<<7|     # Y=16*0
                0<<3|          # X=0
                0)             # 32bit
            bor(r0, r0, r1)
            rotate(broadcast, r3, -NROWS_IDX)
            mov(r1, r5)
            rotate(broadcast, r3, -ROW_IDX)
            isub(r1, r1, r5)
            shl(r1, r1, 7)
            bor(vpmvcd_wr_setup, r0, r1)
            rotate(broadcast, r3, -NROWS_IDX)
            mov(r1, r5)
            rotate(broadcast, r3, -ROW_IDX)
            isub(r1, r1, r5)
            rotate(broadcast, r2, -C_CUR_IDX)
            mov(r0, r5)
            rotate(broadcast, r2, -C_STRIDE_IDX)
            imul24(r1, r1, r5)
            iadd(r0, r0, r1)
            wait_dma_store()
            start_dma_store(r0)

        ldi(null, mask(ROW_IDX), set_flags=True)
        isub(r3, r3, 1, cond='zs')
        jzc(L.dma_for_large_stride_row_store_block_0_loop)
        nop() # delay slot
        nop() # delay slot
        nop() # delay slot
        wait_dma_store()


        # Issue load of block 1
        rotate(broadcast, r3, -LOAD_BLOCKS_IDX)
        mov(r0, r5, set_flags=True)
        jzs(L.dma_for_large_stride_skip_load_block_1)
        nop() # delay slot
        nop() # delay slot
        nop() # delay slot
        ldi(null, mask(LOAD_BLOCKS_IDX), set_flags=True)
        isub(r3, r3, 1, cond='zs')
        nop()

        rotate(broadcast, r3, -NROWS_IDX)
        ldi(null, mask(ROW_IDX), set_flags=True)
        mov(r3, r5, cond='zs')
        nop()
        L.dma_for_large_stride_row_load_block_1_loop
        if True:
            rotate(broadcast, r3, -NCOLS_IDXS[1])
            band(r1, r5, 0xF)
            shl(r1, r1, 4)     # ncols<<4
            bor(r1, r1, 1)     # ncols<<4|1
            shl(r1, r1, 8)     # (ncols<<4|1)<<8
            shl(r1, r1, 8)     # (ncols<<4|1)<<8<<8 = ncols<<20|1<<16
            ldi(r0,
                0x80000000|    # setup_dma_load
                0<<28|         # 32bit
                0<<24|         # use the extended pitch setup register
                1<<12|         # vpitch=1
                0<<11|         # vertical
                (1*16)<<4|     # Y=16*1
                0)             # X=0
            bor(r0, r0, r1)
            rotate(broadcast, r3, -NROWS_IDX)
            mov(r1, r5)
            rotate(broadcast, r3, -ROW_IDX)
            isub(r1, r1, r5)
            shl(r1, r1, 4)
            bor(vpmvcd_rd_setup, r0, r1)
            rotate(broadcast, r3, -NROWS_IDX)
            mov(r1, r5)
            rotate(broadcast, r3, -ROW_IDX)
            isub(r1, r1, r5)
            ldi(r0, 16*4)
            rotate(broadcast, r2, -C_CUR_IDX)
            iadd(r0, r0, r5)
            rotate(broadcast, r2, -C_STRIDE_IDX)
            imul24(r1, r1, r5)
            iadd(r0, r0, r1)
            wait_dma_load()
            start_dma_load(r0)

        ldi(null, mask(ROW_IDX), set_flags=True)
        isub(r3, r3, 1, cond='zs')
        jzc(L.dma_for_large_stride_row_load_block_1_loop)
        nop() # delay slot
        nop() # delay slot
        nop() # delay slot
        wait_dma_load()

        L.dma_for_large_stride_skip_load_block_1

        # Load alpha and beta.
        rotate(r0, r2, -COEF_ADDR_IDX)
        mov(uniforms_address, r0)

        # Setup VPM access for block 1
        setup_vpm_read(mode='32bit vertical', Y=16*1, X=0, nrows=16)
        setup_vpm_write(mode='32bit vertical', Y=16*1, X=0)

        mov(r1, uniform)        # r1=alpha
        mov(broadcast, uniform) # r5=beta

        fmul(rb8, rb8, r1)
        fmul(r0, vpm, r5)
        for i in range(8, 15):
            fadd(vpm, rb[i], r0).fmul(ra[i], ra[i], r1)
            mov(rb[i], 0.0)     .fmul(r0, vpm, r5)
            fadd(vpm, ra[i], r0).fmul(rb[i+1], rb[i+1], r1)
            mov(ra[i], 0.0)     .fmul(r0, vpm, r5)
        fadd(vpm, rb15, r0).fmul(ra15, ra15, r1)
        mov(rb15, 0.0)     .fmul(r0, vpm, r5)
        fadd(vpm, ra15, r0)
        mov(ra15, 0.0)

        # Issue store of block 1
        rotate(broadcast, r3, -STORE_BLOCKS_IDX)
        mov(r0, r5, set_flags=True)
        jzs(L.dma_for_large_stride_skip_store_block_1)
        nop() # delay slot
        nop() # delay slot
        nop() # delay slot
        ldi(null, mask(STORE_BLOCKS_IDX), set_flags=True)
        isub(r3, r3, 1, cond='zs')
        nop()

        rotate(broadcast, r3, -NROWS_IDX)
        ldi(null, mask(ROW_IDX), set_flags=True)
        mov(r3, r5, cond='zs')
        nop()
        L.dma_for_large_stride_row_store_block_1_loop
        if True:
            # store setup params
            mov(r1, 1)
            shl(r1, r1, 7)     # 1<<7
            rotate(broadcast, r3, -NCOLS_IDXS[1])
            bor(r1, r1, r5)    # 1<<7|ncols
            shl(r1, r1, 8)     # (1<<7|ncols)<<8
            shl(r1, r1, 8)     # (1<<7|ncols)<<8<<8 = 1<<23|ncols<<16
            ldi(r0,
                0x80000000|    # setup_dma_store
                1<<14|         # horizontal
                (16*1)<<7|     # Y=16*1
                0<<3|          # X=0
                0)             # 32bit
            bor(r0, r0, r1)
            rotate(broadcast, r3, -NROWS_IDX)
            mov(r1, r5)
            rotate(broadcast, r3, -ROW_IDX)
            isub(r1, r1, r5)
            shl(r1, r1, 7)
            bor(vpmvcd_wr_setup, r0, r1)
            rotate(broadcast, r3, -NROWS_IDX)
            mov(r1, r5)
            rotate(broadcast, r3, -ROW_IDX)
            isub(r1, r1, r5)
            ldi(r0, 16*4)
            rotate(broadcast, r2, -C_CUR_IDX)
            iadd(r0, r0, r5)
            rotate(broadcast, r2, -C_STRIDE_IDX)
            imul24(r1, r1, r5)
            iadd(r0, r0, r1)
            wait_dma_store()
            start_dma_store(r0)

        ldi(null, mask(ROW_IDX), set_flags=True)
        isub(r3, r3, 1, cond='zs')
        jzc(L.dma_for_large_stride_row_store_block_1_loop)
        nop() # delay slot
        nop() # delay slot
        nop() # delay slot
        wait_dma_store()

        L.dma_for_large_stride_skip_store_block_1

        mutex_release()

    L.dma_done

    rotate(broadcast, r2, -J_IDX)
    isub(r0, r5, 1)
    jzc(L.j_loop)   # Jump iz Z-flags are clear
    ldi(null, mask(J_IDX), set_flags=True)  # delay slot
    mov(r2, r0, cond='zs')                  # delay slot
    nop()                                   # delay slot

    rotate(broadcast, r2, -I_IDX)
    isub(r0, r5, 1)
    jzc(L.i_loop)
    ldi(null, mask(I_IDX), set_flags=True)  # delay slot
    mov(r2, r0, cond='zs')                  # delay slot
    nop()                                   # delay slot

    sema_up(COMPLETED)  # Notify completion to the thread 0

    rotate(broadcast, r2, -COEF_ADDR_IDX)
    mov(uniforms_address, r5)
    nop(); nop()
    mov(null, uniform)
    mov(null, uniform)
    mov(null, uniform, set_flags=True)  # thread index

    jzc(L.skip_fin)
    nop()                 # delay slot
    nop()                 # delay slot
    # Only thread 0 enters here.
    iadd(r0, uniform, -1) # delay slot
    L.sem_down
    jzc(L.sem_down)
    sema_down(COMPLETED)  # delay slot  # Wait completion of all threads.
    nop()                 # delay slot
    iadd(r0, r0, -1)      # delay slot

    interrupt()

    L.skip_fin

    # for i in range(32*1024):
    #     nop()

    exit(interrupt=False)

def main():
    with Driver() as drv:
        p = random.randint(64 * 12, 1024)
        q = random.randint(2, 512)
        r = random.randint(64 * 12, 1024)

        assert(q >= 2)

        p_div = 2
        r_div = 6
        n_threads = p_div * r_div

        # Allocate matrices.
        C = drv.alloc((p, r), 'float32')
        A = drv.alloc((p, q), 'float32')
        B = drv.alloc((r, q), 'float32') # Trans Q x R

        # Initialize matrices.
        np.random.seed(0)
        alpha = 1.0
        beta = 1.0
        A[:] = np.random.randn(p, q) # np.ones(shape=(p, q)) #
        B[:] = np.random.randn(r, q) # np.ones(shape=(r, q)) #
        C[:] = np.random.randn(p, r) # np.ones(shape=(p, r)) # np.arange(p*r).reshape(p, r) + 1 #

        # Reference
        RA = A.copy()
        RB = B.copy()
        RC = C.copy()
        start = time.time()
        R = alpha*RA.dot(RB.T) + beta*RC
        elapsed_ref = time.time() - start

        # Allocate uniforms.
        uniforms = drv.alloc((n_threads, 14), 'uint32')
        uniforms[:, 0] = uniforms.addresses()[:, 0]

        th = 0
        p_up = p // 16
        h = (p_up + p_div - 1) // p_div
        h_len = p_div - (h * p_div - p_up)
        r_up = r // 32
        w = (r_up + r_div - 1) // r_div
        w_len = r_div - (w * r_div - r_up)
        h_acc = 0
        for i in range(p_div):
            hi = 0
            if i == p_div-1:
                hi = p - h_acc
            else:
                hi = 16 * h if i < h_len else 16 * (h-1)
            w_acc = 0;
            for j in range(r_div):
                wj = 0
                if j == r_div-1:
                    wj = r - w_acc
                else:
                    wj = 32 * w if j < w_len else 32 * (w-1)
                uniforms[th, 1] = hi
                uniforms[th, 2] = q
                uniforms[th, 3] = wj
                uniforms[th, 4] = A.addresses()[h_acc, 0    ]
                uniforms[th, 5] = B.addresses()[w_acc, 0    ]
                uniforms[th, 6] = C.addresses()[h_acc, w_acc]
                th += 1
                w_acc += wj;
            h_acc += hi;
        uniforms[:, 7] = A.strides[0]
        uniforms[:, 8] = B.strides[0]
        uniforms[:, 9] = C.strides[0]
        uniforms[:, 10] = struct.unpack('L', struct.pack('f', alpha))[0]
        uniforms[:, 11] = struct.unpack('L', struct.pack('f', beta))[0]
        uniforms[:, 12] = np.arange(n_threads)
        uniforms[:, 13] = n_threads

        # Allocate GPU program.
        code = drv.program(sgemm_gpu_code)

        # GPU
        start = time.time()
        drv.execute(
            n_threads=n_threads,
            program=code,
            uniforms=uniforms
        )
        elapsed_gpu = time.time() - start

        # Image.fromarray(R.astype(np.uint8)).save("expected.png")
        # Image.fromarray(C.astype(np.uint8)).save("sgemm.png")

        # np.set_printoptions(threshold=np.inf)
        # print(C.astype(int))

        def Gflops(sec):
            return (2*p*q*r + 3*p*r)/sec * 1e-9

        print('==== sgemm example ({p}x{q} times {q}x{r}) ===='.format(
                p=p, q=q, r=r))
        print('threads: {}'.format(n_threads))
        print('numpy: {:.4f} sec, {:.4f} Gflops'.format(
                elapsed_ref, Gflops(elapsed_ref)))
        print('GPU: {:.4f} sec, {:.4f} Gflops'.format(
                elapsed_gpu, Gflops(elapsed_gpu)))
        print('minimum absolute error: {:.4e}'.format(
                float(np.min(np.abs(R - C)))))
        print('maximum absolute error: {:.4e}'.format(
                float(np.max(np.abs(R - C)))))
        print('minimum relative error: {:.4e}'.format(
                float(np.min(np.abs((R - C) / R)))))
        print('maximum relative error: {:.4e}'.format(
                float(np.max(np.abs((R - C) / R)))))

if __name__ == '__main__':
    if sys.argv[1] == 'qbin':
        print_qbin(sgemm_gpu_code)
        exit(0)
    main()
