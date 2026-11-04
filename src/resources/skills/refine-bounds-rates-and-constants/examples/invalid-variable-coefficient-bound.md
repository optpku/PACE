# Example: a coefficient bound in the wrong direction

Let \(A_0=0\), \(A_{k+1}=A_k+1/M_k\), with \(M_k\ge M_0>0\).
Then \(1/M_k\le1/M_0\), so the assumption yields
\(A_N\le N/M_0\), not the lower bound needed to upper-bound
an error of the form \(C/A_N\).

For \(M_k=2^kM_0\),
\[
A_N=\frac{2}{M_0}(1-2^{-N})<\frac2{M_0}.
\]
Replacing every coefficient by \(M_0\) would incorrectly predict unbounded
linear growth. A correct lower bound requires additional information, such
as \(M_k\le\overline M\), which gives \(A_N\ge N/\overline M\).

Return the missing upper control on the coefficients or a different valid
recurrence comparison. Do not transfer a constant-coefficient rate merely
because the variable coefficients have a positive lower bound.
