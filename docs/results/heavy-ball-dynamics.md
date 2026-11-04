# Heavy-Ball convergence beyond a two-point Lyapunov certificate

[Results](README.md)

**Area:** Momentum methods, Lyapunov certificates, and optimization dynamics.

**Result:** An explicit parameter point has no certificate in the specified two-point linear/quadratic family and no nonconstant finite cycle. A closed parameter box around that point admits a uniform global linear convergence bound.

## Problem

For a $\mu$-strongly convex, $L$-smooth function in an arbitrary finite dimension, consider fixed-parameter Heavy-Ball:

$$
x_{t+1}=x_t-\gamma\nabla f(x_t)+\beta(x_t-x_{t-1}).
$$

The second riddle of Goujaud, Taylor, and Dieuleveut asks whether parameters can admit neither a Lyapunov certificate of their specified form nor a nonconstant periodic orbit, and what the long-term dynamics would then be.

The certificate family uses two adjacent function gaps and inner products of

$$
(s_1,s_2,s_3,s_4)
=\bigl(x_{t-1}-x^\star,\nabla f(x_{t-1}),
x_t-x^\star,\nabla f(x_t)\bigr):
$$

$$
V_t=c_0\bigl(f(x_t)-f^\star\bigr)
+c_1\bigl(f(x_{t-1})-f^\star\bigr)
+\sum_{i,j}Q_{ij}\langle s_i,s_j\rangle.
$$

The coefficients are uniform across the function class, dimensions, and initial points, and must satisfy

$$
f(x_{t+1})-f^\star\le V_{t+1}\le V_t.
$$

A periodic orbit means a nonconstant trajectory of any finite period $K\ge 2$ for one fixed objective.

## Result and scope

Normalize the parameters as $q=\mu/L$, $a=L\gamma$, and $b=\beta$. At

$$
\boxed{(q,a,b)=\left(\frac1{100},\frac{113}{50},\frac35\right),}
$$

the specified two-point linear/quadratic certificate does not exist. At the same point, no nonconstant finite periodic orbit exists for any finite-dimensional $q$-strongly convex, $1$-smooth objective.

Let $\mathcal B$ be the closed box centered at this point with radius $10^{-5}$ in each of the three coordinates. For every parameter in $\mathcal B$, every finite dimension, every fixed objective in the corresponding function class, and every initial pair $(x_{-1},x_0)$, Heavy-Ball converges globally linearly. A uniform explicit estimate is

$$
\lVert x_t-x^\star\rVert
\le 10^7(1-10^{-14})^{t/2}
\sqrt{\lVert x_{-1}-x^\star\rVert^2+\lVert x_0-x^\star\rVert^2}.
$$

Certificate nonexistence is asserted at the central parameter point. The convergence estimate applies throughout the box.

## Why it matters

The result separates global convergence from the reach of this particular two-point certificate family. It gives an explicit globally convergent regime beyond that family and settles the corresponding absence-of-cycles question there.

## References

- Baptiste Goujaud, Adrien Taylor, Aymeric Dieuleveut. *Open Problem: Two Riddles in Heavy-Ball Dynamics*. arXiv:2502.19916, 2025, §3. [Original question](https://arxiv.org/html/2502.19916v1#S3).
- Adrien Taylor, Bryan Van Scoy, Laurent Lessard. *Lyapunov Functions for First-Order Methods: Tight Automated Convergence Guarantees*. ICML 2018, PMLR 80:4897–4906. [Certificate framework](https://proceedings.mlr.press/v80/taylor18a.html).
