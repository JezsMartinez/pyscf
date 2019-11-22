import numpy as np
import ctypes
from pyscf import lib
from pyscf.pbc.tools.pyscf_ase import get_space_group
from pyscf import __config__
from pyscf.pbc.lib import symmetry as symm
from pyscf.pbc.lib.kpts_helper import member

KPT_DIFF_TOL = getattr(__config__, 'pbc_lib_kpts_helper_kpt_diff_tol', 1e-6)
libpbc = lib.load_library('libpbc')

def make_ibz_k(kpts, time_reversal=True):
    '''
    Constructe k points in IBZ
    '''
    nbzkpts = len(kpts.bz_k)
    op_rot = kpts.sg_symm.op_rot_notrans
    kpts.op_rot = op_rot
    kpts.nrot = len(kpts.op_rot)
    if time_reversal:
        kpts.op_rot = np.concatenate([op_rot, -op_rot])

    bz2bz_ks = map_k_points_fast(kpts.bz_k_scaled, op_rot, time_reversal, KPT_DIFF_TOL)

    bz2bz_k = -np.ones(nbzkpts+1, dtype = int)
    ibz2bz_k = []
    for k in range(nbzkpts - 1, -1, -1):
        if bz2bz_k[k] == -1:
            bz2bz_k[bz2bz_ks[k]] = k
            ibz2bz_k.append(k)
    ibz2bz_k = np.array(ibz2bz_k[::-1])
    bz2bz_k = bz2bz_k[:-1].copy()

    bz2ibz_k = np.empty(nbzkpts, int)
    bz2ibz_k[ibz2bz_k] = np.arange(len(ibz2bz_k))
    bz2ibz_k = bz2ibz_k[bz2bz_k]

    kpts.bz2ibz = bz2ibz_k

    kpts.ibz2bz = ibz2bz_k
    kpts.ibz_weight = np.bincount(bz2ibz_k) *(1.0 / nbzkpts)
    kpts.ibz_k_scaled = kpts.bz_k_scaled[kpts.ibz2bz]
    kpts.ibz_k = kpts.cell.get_abs_kpts(kpts.ibz_k_scaled)
    kpts.nibzk = len(kpts.ibz_k)

    for k in range(len(kpts.bz_k)):
        bz_k_scaled = kpts.bz_k_scaled[k]
        ibz_idx = kpts.bz2ibz[k]
        ibz_k_scaled = kpts.ibz_k_scaled[ibz_idx]
        for io in range(len(kpts.op_rot)):
            op = kpts.op_rot[io]
            diff = bz_k_scaled - np.dot(ibz_k_scaled, op.T)
            if (np.absolute(diff) < KPT_DIFF_TOL).all():
                kpts.sym_conn[k] = io
                break

    for i in range(len(kpts.ibz_k)):
        kpts.sym_group.append([])
        ibz_k_scaled = kpts.ibz_k_scaled[i]
        idx = np.where(kpts.bz2ibz == i)[0]
        kpts.bz_k_group.append(idx)
        for j in range(idx.size):
            bz_k_scaled = kpts.bz_k_scaled[idx[j]]
            for io in range(len(kpts.op_rot)):
                op = kpts.op_rot[io]
                diff = bz_k_scaled - np.dot(ibz_k_scaled, op.T)
                if (np.absolute(diff) < KPT_DIFF_TOL).all():
                    kpts.sym_group[i].append(io)
                    break

def map_k_points_fast(bzk_kc, U_scc, time_reversal, tol=1e-7):
    '''
    Find symmetry relations between k-points.
    Adopted from GPAW
    bz2bz_ks[k1,s] = k2 if k1*U.T = k2
    '''
    nbzkpts = len(bzk_kc)

    if time_reversal:
        U_scc = np.concatenate([U_scc, -U_scc])

    bz2bz_ks = -np.ones((nbzkpts, len(U_scc)), dtype=int)

    for s, U_cc in enumerate(U_scc):
        # Find mapped kpoints
        Ubzk_kc = np.dot(bzk_kc, U_cc.T)

        # Do some work on the input
        k_kc = np.concatenate([bzk_kc, Ubzk_kc])
        k_kc = np.mod(np.mod(k_kc, 1), 1)
        aglomerate_points(k_kc, tol)
        k_kc = k_kc.round(-np.log10(tol).astype(int))
        k_kc = np.mod(k_kc, 1)

        # Find the lexicographical order
        order = np.lexsort(k_kc.T)
        k_kc = k_kc[order]
        diff_kc = np.diff(k_kc, axis=0)
        equivalentpairs_k = np.array((diff_kc == 0).all(1), dtype=bool)

        # Mapping array.
        orders = np.array([order[:-1][equivalentpairs_k],
                           order[1:][equivalentpairs_k]])

        # This has to be true.
        assert (orders[0] < nbzkpts).all()
        assert (orders[1] >= nbzkpts).all()
        bz2bz_ks[orders[1] - nbzkpts, s] = orders[0]
    return bz2bz_ks

def aglomerate_points(k_kc, tol):
    '''
    remove numerical error
    Adopted from GPAW
    '''
    nd = k_kc.shape[1]
    nbzkpts = len(k_kc)

    inds_kc = np.argsort(k_kc, axis=0)

    for c in range(nd):
        sk_k = k_kc[inds_kc[:, c], c]
        dk_k = np.diff(sk_k)

        pt_K = np.argwhere(dk_k > tol)[:, 0]
        pt_K = np.append(np.append(0, pt_K + 1), nbzkpts)
        for i in range(len(pt_K) - 1):
            k_kc[inds_kc[pt_K[i]:pt_K[i + 1], c], c] = k_kc[inds_kc[pt_K[i], c], c]

def symmetrize_density(kpts, rhoR_k, ibz_k_idx, mesh):
    '''
    transform real-space densities from IBZ to full BZ
    '''
    rhoR_k = np.asarray(rhoR_k, dtype=np.double, order='C')
    rhoR = np.zeros_like(rhoR_k, dtype=np.double, order='C')

    c_rhoR = rhoR.ctypes.data_as(ctypes.c_void_p)
    c_rhoR_k = rhoR_k.ctypes.data_as(ctypes.c_void_p)

    mesh = np.asarray(mesh, dtype=np.int32, order='C')
    c_mesh = mesh.ctypes.data_as(ctypes.c_void_p)
    for iop in kpts.sym_group[ibz_k_idx]: 
        op = np.asarray(kpts.op_rot[iop], dtype=np.int32, order='C')
        time_reversal = False
        if iop >= kpts.nrot:
            time_reversal = True
            op = -op
        if symm.is_eye(op) or symm.is_inversion(op):
            rhoR += rhoR_k
        else:
            c_op = op.ctypes.data_as(ctypes.c_void_p)
            libpbc.symmetrize(c_rhoR, c_rhoR_k, c_op, c_mesh)
    return rhoR

def transform_mo_coeff(kpts, mo_coeff_ibz):
    '''
    transform MO coefficients from IBZ to full BZ
    '''
    mos = []
    is_uhf = False
    if isinstance(mo_coeff_ibz[0][0], np.ndarray) and mo_coeff_ibz[0][0].ndim == 2:
        is_uhf = True
        mos = [[],[]]
    for k in range(kpts.nbzk):
        ibz_k_idx = kpts.bz2ibz[k]
        iop = kpts.sym_conn[k]
        op = kpts.op_rot[iop]

        def _transform(mo_ibz, iop, op):
            mo_bz = None
            time_reversal = False
            if iop >= kpts.nrot:
                time_reversal = True
                op = -op
            if symm.is_eye(op):
                if time_reversal:
                    mo_bz = mo_ibz.conj()
                else:
                    mo_bz = mo_ibz
            elif symm.is_inversion(op):
                mo_bz = mo_ibz.conj()
            else:
                if iop >= kpts.nrot:
                    iop -= kpts.nrot
                mo_bz = symm.symmetrize_mo_coeff(kpts, mo_ibz, iop)
                if time_reversal:
                    mo_bz = mo_bz.conj()
            return mo_bz

        if is_uhf:
            mo_coeff_a = mo_coeff_ibz[0][ibz_k_idx]
            mos[0].append(_transform(mo_coeff_a, iop, op))
            mo_coeff_b = mo_coeff_ibz[1][ibz_k_idx]
            mos[1].append(_transform(mo_coeff_b, iop, op))
        else:
            mo_coeff = mo_coeff_ibz[ibz_k_idx]
            mos.append(_transform(mo_coeff, iop, op))
    return mos

def transform_dm(kpts, dm_ibz):
    '''
    transform density matrices from IBZ to full BZ
    '''
    dms = []
    is_uhf = False
    if (isinstance(dm_ibz, np.ndarray) and dm_ibz.ndim == 4) or \
       (isinstance(dm_ibz[0][0], np.ndarray) and dm_ibz[0][0].ndim == 2):
        is_uhf = True
        dms = [[],[]]
    for k in range(kpts.nbzk):
        ibz_k_idx = kpts.bz2ibz[k]
        iop = kpts.sym_conn[k]
        op = kpts.op_rot[iop]

        def _transform(dm_ibz, iop, op):
            time_reversal = False
            if iop >= kpts.nrot:
                time_reversal = True
                op = -op
            if symm.is_eye(op):
                if time_reversal:
                    dm_bz = dm_ibz.conj()
                else:
                    dm_bz = dm_ibz
            elif symm.is_inversion(op):
                dm_bz = dm_ibz.conj()
            else:
                if iop >= kpts.nrot:
                    iop -= kpts.nrot
                dm_bz = symm.symmetrize_dm(kpts, dm_ibz, iop)
                if time_reversal:
                    dm_bz = dm_bz.conj()
            return dm_bz

        if is_uhf:
            dm_a = dm_ibz[0][ibz_k_idx]
            dms[0].append(_transform(dm_a, iop, op))
            dm_b = dm_ibz[1][ibz_k_idx]
            dms[1].append(_transform(dm_b, iop, op))
        else:
            dm = dm_ibz[ibz_k_idx]
            dms.append(_transform(dm, iop, op))
    if is_uhf:
        nkpts = len(dms[0])
        nao = dms[0][0].shape[0]
        return lib.asarray(dms).reshape(2,nkpts,nao,nao)
    else:
        return lib.asarray(dms)

def transform_mo_energy(kpts, mo_energy):
    '''
    transform mo_energy from IBZ to full BZ
    '''
    is_uhf = False
    if isinstance(mo_energy[0][0], np.ndarray):
        is_uhf = True
    mo_energy_bz = []
    if is_uhf:
        mo_energy_bz = [[],[]]
    for k in range(kpts.nbzk):
        ibz_k_idx = kpts.bz2ibz[k]
        if is_uhf:
            mo_energy_bz[0].append(mo_energy[0][ibz_k_idx])
            mo_energy_bz[1].append(mo_energy[1][ibz_k_idx])
        else: 
            mo_energy_bz.append(mo_energy[ibz_k_idx])
    return mo_energy_bz


def check_mo_occ_symmetry(kpts, mo_occ):
    '''
    check if mo_occ has the correct symmetry
    '''
    for k in range(kpts.nibzk):
        bz_k = kpts.bz_k_group[k]
        nbzk = bz_k.size
        for i in range(nbzk):
            for j in range(i+1,nbzk):
                if not (np.absolute(mo_occ[bz_k[i]] - mo_occ[bz_k[j]]) < KPT_DIFF_TOL).all():
                    raise RuntimeError("symmetry broken")
    mo_occ_ibz = []
    for k in range(kpts.nibzk):
        mo_occ_ibz.append(mo_occ[kpts.ibz2bz[k]])
    return mo_occ_ibz


class KPoints():
    '''
    This class handles k-point symmetries etc.
    '''
    def __init__(self, cell, kpts, point_group = True):

        self.cell = cell
        self.sg_symm = symm.Symmetry(cell, point_group)

        self.bz_k_scaled = cell.get_scaled_kpts(kpts)
        self.bz_k = kpts
        self.bz_weight = np.asarray([1./len(kpts)]*len(kpts))
        self.bz2ibz = np.arange(len(kpts), dtype=int)

        self.ibz_k_scaled = self.bz_k_scaled
        self.ibz_k = kpts
        self.ibz_weight = np.asarray([1./len(kpts)]*len(kpts))
        self.ibz2bz = np.arange(len(kpts), dtype=int)

        self.op_rot = np.eye(3,dtype =int).reshape(1,3,3)
        self.nrot = 1
        self.sym_conn = np.zeros(len(kpts), dtype = int)
        self.sym_group = []
        self.bz_k_group = []

        self._nbzk = len(self.bz_k)
        self._nibzk = len(self.ibz_k)

    @property
    def nbzk(self):
        return self._nbzk

    @nbzk.setter
    def nbzk(self, n):
        self._nbzk = n

    @property
    def nibzk(self):
        return self._nibzk

    @nibzk.setter
    def nibzk(self, n):
        self._nibzk = n

    def build_kptij_lst(self):
        '''
        Build k-point-pair list for SCF calculations
        All combinations:
            k_ibz  k_ibz
            k_ibz  k_bz
            k_bz   k_bz
        '''
        kptij_lst = [(self.bz_k[i], self.bz_k[i]) for i in range(self.nbzk)]
        for i in range(self.nibzk):
            ki = self.ibz_k[i]
            where = member(ki, self.bz_k)
            for j in range(self.nbzk): 
                kj = self.bz_k[j]
                if not j in where:
                    kptij_lst.extend([(ki,kj)])
        kptij_lst = np.asarray(kptij_lst)
        return kptij_lst

    make_ibz_k = make_ibz_k
    symmetrize_density = symmetrize_density
    transform_mo_coeff = transform_mo_coeff
    transform_dm = transform_dm
    transform_mo_energy = transform_mo_energy
    check_mo_occ_symmetry = check_mo_occ_symmetry