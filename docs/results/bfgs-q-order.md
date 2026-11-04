# Exact-line-search BFGS: the minimum Q-order is one

[Results](README.md)

**Area:** Quasi-Newton methods and asymptotic convergence.

**Result:** In every fixed dimension $n\ge 2$, the least possible adjacent-iterate Q-order is $\rho_n=1$, and one smooth, strongly convex objective with one nonterminating BFGS trajectory attains it.

## Problem

Let $F:\mathbb{R}^n\to\mathbb{R}$ be $C^\infty$, with $n\ge 2$. Consider Hessian-form BFGS:

$$
d_k=-B_k^{-1}\nabla F(x_k),\qquad
x_{k+1}=x_k+\alpha_k d_k,
$$

where $\alpha_k$ globally minimizes $F(x_k+\alpha d_k)$ over the entire search line. With $s_k=x_{k+1}-x_k$ and $y_k=\nabla F(x_{k+1})-\nabla F(x_k)$, the update is

$$
B_{k+1}
=B_k-\frac{B_ks_ks_k^\top B_k}{s_k^\top B_ks_k}
+\frac{y_ky_k^\top}{s_k^\top y_k}.
$$

Restrict attention to nonterminating trajectories for which every step is defined, $B_k$ is positive definite, $s_k^\top y_k>0$, and $x_k$ converges to a locally strongly convex minimizer $x^\star$. Define $e_k=\lVert x_k-x^\star\rVert$ and

$$
q=\sup\left\{p>0:\ \exists C<\infty,\ k_0,\quad
e_{k+1}\le C e_k^p\ \text{for all }k\ge k_0\right\}.
$$

What is the infimum $\rho_n$ of these Q-orders in each fixed dimension? Can a single fixed objective and a single fixed trajectory attain the infimum?

## Result and scope

For every fixed integer $n\ge 2$,

$$
\boxed{\rho_n=1,\quad\text{with the infimum attained}.}
$$

The construction gives a globally $C^\infty$, globally strongly convex objective with a unique minimizer and a nonterminating exact-line-search BFGS trajectory of Q-order exactly one. It is constructed in two dimensions and extended to higher dimensions by adding independent quadratic terms.

The trajectory is Q-superlinear:

$$
\frac{e_{k+1}}{e_k}\longrightarrow 0.
$$

Nevertheless, for every $p>1$ and every finite $C$, arbitrarily late iterates satisfy $e_{k+1}>C e_k^p$.

## Why it matters

Q-superlinear convergence does not imply any fixed power-law order greater than one, even for infinitely differentiable, strongly convex objectives. The result exhibits this distinction along one fixed trajectory.

The question originates in Powell's work on exact-line-search variable-metric methods. Yuan established an infimum of one in a $C^2$ convex setting using a family of orders approaching one. The related 2026 paper listed below treats attainment in the smooth, strongly convex setting.

## References

- M. J. D. Powell. *On the Rate of Convergence of Variable Metric Algorithms for Unconstrained Optimization*. Proceedings of the International Congress of Mathematicians, Warszawa 1983, Vol. 2, pp. 1525–1539, 1984. [Author's publication list](https://www.damtp.cam.ac.uk/user/na/MJDP_Offprints.html).
- Y. Yuan. *On the Least Q-order of Convergence of Variable Metric Algorithms*. IMA Journal of Numerical Analysis, 4(2):233–239, 1984. [Paper](https://lsec.cc.ac.cn/pub/yyx/papers/1984IMAJNA-4-233-239.pdf).
- Benqi Liu, Chenyi Li, Zaiwen Wen. *The Minimum Q-Order of BFGS with Exact Line Search Is One*. arXiv:2609.00596, 2026. [Related paper](https://arxiv.org/abs/2609.00596).
