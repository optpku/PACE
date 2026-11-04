# Matching sample lower bounds on a fixed path network

[Results](README.md)

**Area:** Decentralized stochastic optimization and oracle complexity.

**Result:** On a fixed lazy path, local linear-span methods must pay both the statistical estimation cost and the network propagation cost to make every node's output accurate.

## Problem

Let $M$ nodes solve

$$
\min_x f(x),\qquad f(x)=\frac1M\sum_{i=1}^M f_i(x).
$$

Each $f_i$ is convex and $L$-smooth; the average $f$ is $\mu$-strongly convex. The nodes start at a common point with initial function gap at most $\Delta_0$. Local stochastic gradients are unbiased, have conditional variance at most $\sigma^2$, and are conditionally independent across nodes within a round. Write $\rho$ for the gossip matrix's spectral gap.

In the nonblocking model, each node makes one gradient query and participates in one gossip multiplication per round. Thus $H$ rounds use $N=MH$ samples. Accuracy is required at every node:

$$
\max_i\mathbb E\!\left[f(\widehat x_i)-f(x^\star)\right]\le\varepsilon.
$$

The strongly convex restart upper bound of Kluger, Attia, and Koren has the principal scale

$$
N=\widetilde O\!\left(
\frac{\sigma^2}{\mu\varepsilon}
+M\sqrt{\frac{L}{\mu\rho}}\log\frac{\Delta_0}{\varepsilon}
\right).
$$

Are both terms unavoidable for local linear-span first-order methods under this per-round accounting and local-output requirement?

## Result and scope

Fix $M\ge 2$ and the lazy path gossip matrix

$$
P=I-\frac14 L_{\mathrm{path}},\qquad
\rho=\sin^2\!\left(\frac{\pi}{2M}\right),
$$

where $L_{\mathrm{path}}$ is the path graph Laplacian. For $L/\mu\ge 4$ and $0<\varepsilon\le\Delta_0/2$, every algorithm in the stated class has a legal instance requiring

$$
\boxed{
N\ge c\left[
\frac{\sigma^2}{\mu\varepsilon}
+M\sqrt{\frac{L}{\mu\rho}}\log\frac{\Delta_0}{\varepsilon}
\right],
}
$$

for a universal $c>0$. Equivalently, the number of rounds per node satisfies

$$
H\ge c\left[
\frac{\sigma^2}{M\mu\varepsilon}
+\sqrt{\frac{L}{\mu\rho}}\log\frac{\Delta_0}{\varepsilon}
\right].
$$

The algorithms may choose linear-combination coefficients from their complete local histories and know the network matrix and its spectrum in advance. The claim concerns the fixed path and the local linear-span class described above.

## Construction and significance

A Gaussian location-estimation instance gives the statistical lower bound. A strongly convex quadratic chain that advances alternately from opposite ends of the path gives the network lower bound. Both constructions can satisfy

$$
\nabla f_i(x^\star)=0\qquad\text{for every node }i,
$$

so the obstruction persists with zero local heterogeneity at the optimum.

The lower bound matches the principal polynomial dependencies of the existing strongly convex restart upper bound. It identifies the joint scale of statistical parallelism and network propagation in this fixed-path model.

## References

- Nitai Kluger, Amit Attia, Tomer Koren. *Near-Optimal Decentralized Stochastic Convex Optimization over Networks*. arXiv:2606.04757, 2026. [Appendix D: strongly convex restart upper bound](https://arxiv.org/html/2606.04757v1#A4); [paper, including the linear-span lower-bound model in Appendix E](https://arxiv.org/html/2606.04757v1).
- Kevin Scaman, Francis Bach, Sébastien Bubeck, Yin Tat Lee, Laurent Massoulié. *Optimal Algorithms for Smooth and Strongly Convex Distributed Optimization in Networks*. ICML 2017, PMLR 70:3027–3036. [Paper](https://proceedings.mlr.press/v70/scaman17a.html).
