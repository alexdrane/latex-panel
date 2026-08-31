# Full Likelihood and Jeffreys Priors — Q3

## Generative model

The observations are:

$$y_{1,j} = f(t_j) + g_1(t_j) + \epsilon_{1,j}, \qquad j=1,\ldots,N$$
$$y_{2,j} = f(t_j - \Delta t) + \Delta m + g_2(t_j) + \epsilon_{2,j}$$

where $f, g_1, g_2$ are independent GPs and $\epsilon_{i,j}\sim\mathcal{N}(0,\sigma_{i,j}^2)$ are independent noise. Since sums of independent Gaussians are Gaussian, the joint vector:

$$\mathbf{y} = \begin{pmatrix}\mathbf{y}_1\\\mathbf{y}_2\end{pmatrix} \sim \mathcal{N}(\boldsymbol{\mu},\, K)$$

## Mean vector

$$E[y_{1,j}] = c, \qquad E[y_{2,j}] = c + \Delta m$$

$$\boldsymbol{\mu} = \begin{pmatrix}c\,\mathbf{1}_N\\ (c+\Delta m)\,\mathbf{1}_N\end{pmatrix}$$

Only $c$ and $\Delta m$ enter $\boldsymbol{\mu}$. Note $\Delta t$ does **not** appear here.

## Covariance matrix

Using independence of $f$, $g_1$, $g_2$, and the noise:

$$K = \begin{pmatrix}K_{11} & K_{12}\\ K_{12}^T & K_{22}\end{pmatrix}$$

**Image 1 block** $(j,j'$ entries$)$:

$$[K_{11}]_{jj'} = \underbrace{A_f^2 e^{-|t_j-t_{j'}|/\tau_f}}_{\text{Cov}[f(t_j),f(t_{j'})]} + \underbrace{A_g^2 e^{-(t_j-t_{j'})^2/2\tau_g}}_{\text{Cov}[g_1(t_j),g_1(t_{j'})]} + \underbrace{\sigma_{1,j}^2\delta_{jj'}}_{\text{noise}}$$

**Image 2 block**:

$$[K_{22}]_{jj'} = A_f^2 e^{-|t_j-t_{j'}|/\tau_f} + A_g^2 e^{-(t_j-t_{j'})^2/2\tau_g} + \sigma_{2,j}^2\delta_{jj'}$$

(the OU kernel is shift-invariant: $k_f(t_j-\Delta t,\, t_{j'}-\Delta t) = k_f(t_j, t_{j'})$)

**Cross block** — only $f$ contributes since $g_1\perp g_2$:

$$[K_{12}]_{jj'} = \mathrm{Cov}[f(t_j),\, f(t_{j'}-\Delta t)] = A_f^2\,e^{-|t_j - t_{j'} + \Delta t|/\tau_f}$$

$\Delta t$ appears **only** here. So the parameter split is:

- **Mean parameters** $\boldsymbol{\alpha} = (c,\,\Delta m)$: enter $\boldsymbol{\mu}$ only
- **Kernel parameters** $\boldsymbol{\beta} = (\Delta t,\, A_f,\, \tau_f)$: enter $K$ only

## Full log-likelihood

$$\ln L(\boldsymbol{\theta}) = -\frac{1}{2}\ln\det K(\boldsymbol{\beta}) - \frac{1}{2}(\mathbf{y}-\boldsymbol{\mu}(\boldsymbol{\alpha}))^T K(\boldsymbol{\beta})^{-1}(\mathbf{y}-\boldsymbol{\mu}(\boldsymbol{\alpha})) + \text{const}$$

## Fisher matrix block structure

**Cross block $\mathcal{I}_{\alpha_k,\beta_l} = 0$:**

$$\frac{\partial \ln L}{\partial \alpha_k} = \left(\frac{\partial\boldsymbol{\mu}}{\partial\alpha_k}\right)^T K^{-1}(\mathbf{y}-\boldsymbol{\mu})$$

$$\frac{\partial^2\ln L}{\partial\alpha_k\,\partial\beta_l} = \left(\frac{\partial\boldsymbol{\mu}}{\partial\alpha_k}\right)^T\frac{\partial K^{-1}}{\partial\beta_l}(\mathbf{y}-\boldsymbol{\mu})$$

Taking expectation: $E[\mathbf{y}-\boldsymbol{\mu}]=\mathbf{0}$, so $\mathcal{I}_{\alpha_k,\beta_l} = 0$ exactly.

Therefore $\mathcal{I}$ is block-diagonal and $P(\boldsymbol{\theta})\propto\sqrt{\det\mathcal{I}_{\boldsymbol{\alpha\alpha}}}\cdot\sqrt{\det\mathcal{I}_{\boldsymbol{\beta\beta}}}$.

## Jeffreys priors

**$c$ and $\Delta m$** are location parameters: $\boldsymbol{\mu}\to\boldsymbol{\mu}+\epsilon\,\partial\boldsymbol{\mu}/\partial\alpha_k$ under $\alpha_k\to\alpha_k+\epsilon$.

$$\mathcal{I}_{\alpha_k\alpha_l} = \left(\frac{\partial\boldsymbol{\mu}}{\partial\alpha_k}\right)^T K^{-1}\frac{\partial\boldsymbol{\mu}}{\partial\alpha_l} = \text{const w.r.t. }\boldsymbol{\alpha}$$

so $P_J(c)\propto 1$, $P_J(\Delta m)\propto 1$.

**$\Delta t$** acts as a location shift on image 2's time axis. Under $\Delta t\to\Delta t+\epsilon$, the argument $|t_j - t_{j'} + \Delta t|$ shifts by $\epsilon$ — the same structure as a location parameter, giving $P_J(\Delta t)\propto 1$.

**$A_f$** is a scale parameter: $K\to\lambda^2 K$ under $A_f\to\lambda A_f$, so $\mathcal{I}(A_f)\propto A_f^{-2}$ and:

$$P_J(A_f)\propto\frac{1}{A_f}$$

**$\tau_f$** is a scale parameter for the time axis: $\mathcal{I}(\tau_f)\propto\tau_f^{-2}$ and:

$$P_J(\tau_f)\propto\frac{1}{\tau_f}$$

## Result

$$\boxed{P(\boldsymbol{\theta}) \propto \frac{1}{A_f\,\tau_f}}$$

