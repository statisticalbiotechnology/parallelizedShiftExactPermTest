from score_initialization import score_initialization
get_score_init = score_initialization().get_score_init
import numpy as np
from numba import cuda
from get_permutations import get_permutationsA_u4_v_u2, get_permutationsA_f8_v_u2, get_permutationsA_f8_v_u4

class significance_of_mean_cuda(object):
    """Fast p-value calculation.
    Credit:
        Relevant githubs:
            Micheal H: https://github.com/hoehleatsu/permtest
            Lukas Käll: https://github.com/statisticalbiotechnology/exactpermutation
        Relevant articles:
            Bert Green: A Practical Interactive Program for Randomization Tests of Location
            Marcello Pagano & David Tritchler: On Obtaining Permutation Distributions in Polynomial Time
            Jens Gebhard and Norbert Schmitz: Permutation tests- a revival?! II. An efficient algorithm for computing the critical region 
    """
    def __init__(self,num_bin = None, dtype_v=np.uint64, dtype_A=np.float64):
        """
        Args:
            num_bin (int): NThe number of bins to divide each sample-set.
            dtype_v (type): The datatype of small arrays and values.
            dtype_A (type): The datatype type of large arrays.
        """
        self.num_bin = num_bin
        self.dtype_v = dtype_v
        self.dtype_A = dtype_A
        if self.dtype_v == np.uint16 and self.dtype_A == np.uint32:
            self.get_perm = get_permutationsA_u4_v_u2
        elif self.dtype_v == np.uint16 and self.dtype_A == np.float64:
            self.get_perm = get_permutationsA_f8_v_u2
        elif self.dtype_v == np.uint32 and self.dtype_A == np.float64:
            self.get_perm = get_permutationsA_f8_v_u4
        else:
            raise ValueError("The selected value tkype combination is currently not available!")
         

    def table(self,val, S):
        """Method to set initial values for sub-array according to the digitization of samples.

        Args:
            val (list): Digitized values.
            S(int): Sum up to :m

        Returns:
            Initialized initial sub-array.
        """
        T = np.zeros(S + 1)
        for e in val:
            T[int(e)] +=1
        return T

    def _get_digitized_score(self, X, bins):
        """Digitize the values for each sample.

        Args:
            X (array): Concatenated sample from original samples A and B.
            bins(int): The number of bins to divide the sample values.

        Returns:
            digitized array
        """
        digitized = np.zeros(X.shape,dtype=self.dtype_v)
        for i, (x,b) in enumerate(zip(X,bins)):
            digitized[i,:] = np.digitize(x, b).astype(self.dtype_v) - 1
        return digitized

    def _init_A0(self, A0, S, z, n_samples):
        """Initialize the first array(A0)
        Args:
            A0 (array): Empty array to initialize.
            S(int): Sum up to :m.
            z(array): Digitized array.
            n_samples(int): The total number of samples.

        Returns:
            Initialized A0 array
        """
        for i in range(n_samples):
            A0[:int(S[int(i)]) + 1,0,i] = self.table(z[i,0, 0:1],int(S[int(i)]))
        return A0

    def _ensure_contiguous(self, z, S, A0, A1):
        """Assert all arrays are contiguous.
        Args:
            A0 (array): Initialize A0 array.
            A1 (array): Second array to start fill.
            S(int): Sum up to :m.
            z(array): Digitized array.
        Returns:
            Contiguous arrays.
        """
        return (np.ascontiguousarray(z, self.dtype_v), np.ascontiguousarray(S, self.dtype_v),
                np.ascontiguousarray(A0, self.dtype_A), np.ascontiguousarray(A1, self.dtype_A))

    def _load_gpu(self, z, S, A0, A1):
        """Load arrays onto the GPU's.
        Args:
            z (array): Digitized array.
            A0 (array): Initialized A0 array.
            A1 (array): Second array to fill.
            S(int): Sum up to :m.
        Returns:
            GPU arrays.
        """
        stream = cuda.stream()
        return (stream, cuda.to_device(z, stream), cuda.to_device(S, stream),
                cuda.to_device(A0, stream), cuda.to_device(A1, stream))

    def _run_calculations(self, dA0, dA1, dz, dS, length, blockdim, griddim, stream):
        """Start to fill the rest of working array.
        Args:
            dz (array): Digitized GPU-array.
            dA0 (array): Initialized A0 GPU-array.
            dA1 (array): A1 GPU-array.
            dS(int): Sum up to :m. GPU-array
            blockdim(tripple): Dimension of GPU-block
            griddim(tripple): Dimension of GPU-grid
            stream: GPU-stream
        Returns:
            The two last calculated sub-arrays (onto the GPU), dA0 and dA1.
        """
        for i,k in enumerate(range(2, length + 1)):
            dk = self.dtype_v(k)
            self.get_perm[griddim,blockdim, stream](dA0,dA1, dk,dz, dS)
            tmp = dA1
            dA1 = dA0
            dA0 = tmp
        return dA0, dA1

    def _get_calculated_array(self, dA0, A1, stream, m):
        """Retrieve the final subarray to host.
        Args:
            dA0 (array): Initialized A0 GPU-array.
            dA1 (array): A1 GPU-array.
            m (int): Length of sample A.
            stream: GPU-stream.
        Returns:
            Returns the necessary part for the p-value calculation from the final sub-array.
        """
        dA0.to_host(stream)
        stream.synchronize()
        return A1[:,m-1,:]

    def _calculate_p_values(self, Z, n_samples, S, A ,bins):
        """Calculate p-value for each sub-array
        Args:
            Z (array): The necessary part of the array to calculate sample p-values.
            n_samples (int): The total number of samples.
            S(int): Sum up to :m.
            A(array): The Values from sample A.
            bins(array): Bins for digitization.
        Returns:
            p-values
        """
        P = np.zeros(n_samples)
        for i, (a,b) in enumerate(zip(A,bins)):
            pmf = Z[:,i] / np.sum(Z[:,i])
            a_ = np.digitize(a, b).astype(self.dtype_v) - 1
            P[i] = np.sum(pmf[int(sum(a_)):(int(S[i])+1)])
        return P

    def exact_perm_numba_shift(self, m, n, S, z):
        """Run the shift-method on the GPU.
        Args:
            m (int): Sample size of sample A
            n (int): Sample size of sample B
            S(int): Sum up to :m.
            z (array): Digitized array.
        Returns:
             A necessary part of the calculated array to retrieve p-values.
        """
        n_samples = z.shape[0]

        A0 = np.zeros([int(np.max(S)) + 1, m, n_samples], self.dtype_A)

        NN, NM, _ = A0[:,:,:].shape


        blockdim = (64, 3, 2)
        griddim = (int(np.ceil((NN )/ blockdim[0])),
                   int(np.ceil(NM/blockdim[1] + 1)),
                   int(np.ceil(n_samples/blockdim[2] + 1)))

        z = get_score_init(z)

        A0 = self._init_A0(A0,S,z,n_samples)

        A1 = np.zeros([int(np.max(S)) + 1, m, n_samples], self.dtype_A)

        z,S,A0,A1 = self._ensure_contiguous(z,S,A0,A1)

        stream, dz, dS, dA0, dA1 = self._load_gpu(z,S,A0,A1)

        dA0, dA1 = self._run_calculations(dA0,dA1,dz, dS, m+n, blockdim, griddim, stream)
        return self._get_calculated_array(dA0,A1, stream, m)

    def run(self, A, B):
        """Run method on the GPU.
        Args:
            A (array): Samples A.
            B (array): Samples B.
        Returns:
             p-values
        """
        m = A.shape[1]
        n = B.shape[1]

        n_samples = A.shape[0]

        X = np.concatenate([A,B],axis=1)
        X.sort()

        if not self.num_bin:
            self.num_bin = np.ceil(max(z)) - np.floor(min(z)) + 1

        bins = np.linspace(np.min(X, axis=1), np.max(X, axis=1), self.num_bin, axis=1)

        digitized = self._get_digitized_score(X, bins)

        S = np.sum(digitized[:,m:],axis=1)

        Z = self.exact_perm_numba_shift(int(m), int(n), S, digitized)

        return self._calculate_p_values(Z, n_samples, S, A ,bins)