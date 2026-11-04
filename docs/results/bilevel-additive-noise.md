# Stochastic bilevel optimization under additive noise

[Results](README.md)

**Area:** Nonconvex–strongly convex bilevel optimization and first-order query complexity.

**Result:** Under state-independent additive noise and the specified one- or two-state sampling oracle, the optimal precision exponent is two:

$$
c\varepsilon^{-2}\le Q^\star(\varepsilon)
\le C\varepsilon^{-2}\log(e/\varepsilon).
$$

## Problem

Consider

$$
\min_x\Phi(x),\qquad
\Phi(x)=f(x,y^\star(x)),\qquad
y^\star(x)=\operatorname*{argmin}_y g(x,y),
$$

where $f=\mathbb E[F]$, $g=\mathbb E[G]$, and $g(x,\cdot)$ is $\mu$-strongly convex. Fix $\mu>0$, $L_0>\mu$, $\sigma>0$, and $\Delta>0$.

For the joint state $u=(x,y)$, the population functions are twice continuously differentiable and satisfy the global bounds

$$
\lVert\nabla^2f(u)\rVert_{\mathrm{op}}
+\lVert\nabla^2g(u)\rVert_{\mathrm{op}}\le L_0,
$$

$$
\lVert\nabla^2f(u)-\nabla^2f(v)\rVert_{\mathrm{op}}
+\lVert\nabla^2g(u)-\nabla^2g(v)\rVert_{\mathrm{op}}
\le L_0\lVert u-v\rVert,
$$

$$
\lVert\nabla_y f(u)\rVert
+\lVert\nabla^2_{xy}g(u)\rVert_{\mathrm{op}}\le L_0.
$$

Initialization is $x_0=y_0=0$, with $y^\star(0)=0$ and $\Phi(0)-\inf\Phi\le\Delta$.

### Oracle and accounting

Only the four stochastic partial-gradient blocks $\nabla_xF$, $\nabla_yF$, $\nabla_xG$, and $\nabla_yG$ are available. Each fresh sample is used for one selected block, evaluated at one or two states specified before drawing that sample. **Every returned block value is charged.**

Each block is unbiased, its noise has second moment at most $\sigma^2$, and its centered noise for the same sample is state-independent. For any queried block $B$,

$$
\mathbb E\left\lVert
\bigl[B(u;\omega)-\mathbb EB(u;\omega)\bigr]
-\bigl[B(v;\omega)-\mathbb EB(v;\omega)\bigr]
\right\rVert^2=0.
$$

What is the optimal worst-case expected query count $Q^\star(\varepsilon)$ for producing

$$
\mathbb E\lVert\nabla\Phi(\widehat x)\rVert^2\le\varepsilon^2
$$

over all legal instances and finite dimensions? Algorithms may be randomized and adaptive, with almost-sure stopping.

## Result and scope

For every fixed legal parameter tuple, constants $c,C,\varepsilon_0>0$ depending only on that tuple satisfy, for $0<\varepsilon\le\varepsilon_0$,

$$
\boxed{
c\varepsilon^{-2}\le Q^\star(\varepsilon)
\le C\varepsilon^{-2}\log(e/\varepsilon).
}
$$

Thus $Q^\star(\varepsilon)=\widetilde\Theta(\varepsilon^{-2})$, with a single logarithmic gap between the displayed bounds.

The rate is for the stated additive-noise oracle, including its same-sample two-state evaluations and per-return accounting.

## Algorithmic idea and significance

The upper bound estimates population gradients at a fixed anchor and uses exact noise cancellation in same-sample two-point differences to recover gradient differences elsewhere. These operations support the inner solve, an approximate adjoint solve, and hypergradient estimation, followed by outer descent. The total count includes anchor sampling, both returns of paired evaluations, finite differences, and all inner iterations.

A legal one-dimensional Gaussian location family supplies the lower bound, covering adaptive randomized algorithms, almost-sure stopping, and arbitrary outputs.

Together, the bounds identify the optimal precision exponent two in this state-independent additive-noise model. The related first-order bilevel literature below provides the broader oracle-complexity context.

## References

- Jeongyeol Kwon, Dohyun Kwon, Stephen Wright, Robert D. Nowak. *A Fully First-Order Method for Stochastic Bilevel Optimization*. ICML 2023, PMLR 202:18083–18113. [Paper](https://proceedings.mlr.press/v202/kwon23c.html).
- Jeongyeol Kwon, Dohyun Kwon, Hanbaek Lyu. *On The Complexity of First-Order Methods in Stochastic Bilevel Optimization*. ICML 2024, PMLR 235:25784–25811. [Paper](https://proceedings.mlr.press/v235/kwon24b.html).
