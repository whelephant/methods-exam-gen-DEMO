-- VCE Mathematical Methods — Units 3 & 4 — verbatim AoS dot points.
-- Source: VCE Mathematical Methods Study Design 2023–2027, pp. 98–101.
-- Adapted from mathematical_methods/study_design/aos_dot_points_units_3_4.sql for SQLite.
-- Schema is created by 001_schema.sql. Apply that first.
-- Idempotent: safe to re-run.
--
-- Bullet text uses LaTeX delimited by $...$ for inline math (rendered with KaTeX on the frontend).

-- ─── AREAS ───────────────────────────────────────────────────────────

insert into study_areas (subject, aos, title, intro) values
  ('mathematical_methods', 1, 'Functions, relations and graphs',
   'In this area of study students cover transformations of the plane and the behaviour of some elementary functions of a single real variable, including key features of their graphs such as axis intercepts, stationary points, points of inflection, domain (including maximal, implied or natural domain), co-domain and range, asymptotic behaviour and symmetry. The behaviour of functions and their graphs is to be explored in a variety of modelling contexts and theoretical investigations.'),
  ('mathematical_methods', 2, 'Algebra, number and structure',
   'In this area of study students cover the algebra of functions, including composition of functions, inverse functions and the solution of equations. They also study the identification of appropriate solution processes for solving equations, and systems of simultaneous equations, presented in various forms. Students also cover recognition of equations and systems of equations that are solvable using inverse operations or factorisation, and the use of graphical and numerical approaches for problems involving equations where exact value solutions are not required, or which are not solvable by other methods. This content is to be incorporated as applicable to the other areas of study.'),
  ('mathematical_methods', 3, 'Calculus',
   'In this area of study students cover graphical treatment of limits, continuity and differentiability of functions of a single real variable, and differentiation, anti-differentiation and integration of these functions. This material is to be linked to applications in practical situations.'),
  ('mathematical_methods', 4, 'Data analysis, probability and statistics',
   'In this area of study students cover discrete and continuous random variables, their representation using tables, probability functions (specified by rule and defining parameters as appropriate); the calculation and interpretation of central measures and measures of spread; and statistical inference for sample proportions. The focus is on understanding the notion of a random variable, related parameters, properties and application and interpretation in context for a given probability distribution.')
on conflict (subject, aos) do update set title = excluded.title, intro = excluded.intro;

-- ─── POINTS ──────────────────────────────────────────────────────────

insert into study_points (subject, aos, is_header, text, sort_order) values
  -- AoS 1 — Functions, relations and graphs
  ('mathematical_methods', 1, 0, 'graphs of polynomial functions and their key features', 1),
  ('mathematical_methods', 1, 0, 'graphs of the following functions: power functions, $y = x^n$, $n \in \mathbb{Q}$; exponential functions, $y = a^x$, $a \in \mathbb{R}^+$, in particular $y = e^x$; logarithmic functions, $y = \log_e(x)$ and $y = \log_{10}(x)$; and circular functions, $y = \sin(x)$, $y = \cos(x)$ and $y = \tan(x)$ and their key features', 2),
  ('mathematical_methods', 1, 0, 'transformation from $y = f(x)$ to $y = Af(n(x+b)) + c$, where $A, n, b$ and $c \in \mathbb{R}$, $A, n \neq 0$, and $f$ is one of the functions specified above, and the inverse transformation', 3),
  ('mathematical_methods', 1, 0, 'the relation between the graph of an original function and the graph of a corresponding transformed function (including families of transformed functions for a single transformation parameter)', 4),
  ('mathematical_methods', 1, 0, 'graphs of sum, difference, product and composite functions involving functions of the types specified above (not including composite functions that result in reciprocal or quotient functions)', 5),
  ('mathematical_methods', 1, 0, 'modelling of practical situations using polynomial, power, circular, exponential and logarithmic functions, simple transformation and combinations of these functions, including simple piecewise (hybrid) functions.', 6),

  -- AoS 2 — Algebra, number and structure
  ('mathematical_methods', 2, 0, 'solution of polynomial equations with real coefficients of degree $n$ having up to $n$ real solutions, including numerical solutions', 1),
  ('mathematical_methods', 2, 0, 'functions and their inverses, including conditions for the existence of an inverse function, and use of inverse functions to solve equations involving exponential, logarithmic, circular and power functions', 2),
  ('mathematical_methods', 2, 0, 'composition of functions, where $f$ composite $g$, $f \circ g$, is defined by $(f \circ g)(x) = f(g(x))$ given $r_g \subseteq d_f$', 3),
  ('mathematical_methods', 2, 0, 'solution of equations of the form $f(x) = g(x)$ over a specified interval, where $f$ and $g$ are functions of the type specified in the ''Functions, relations and graphs'' area of study, by graphical, numerical and algebraic methods, as applicable', 4),
  ('mathematical_methods', 2, 0, 'solution of literal equations and general solution of equations involving a single parameter', 5),
  ('mathematical_methods', 2, 0, 'solution of simple systems of simultaneous linear equations, including consideration of cases where no solution or an infinite number of possible solutions exist (geometric interpretation only required for two equations in two variables).', 6),

  -- AoS 3 — Calculus
  ('mathematical_methods', 3, 0, 'deducing the graph of the derivative function from the graph of a given function and deducing the graph of an anti-derivative function from the graph of a given function', 1),
  ('mathematical_methods', 3, 0, 'derivatives of $x^n$ for $n \in \mathbb{Q}$, $e^x$, $\log_e(x)$, $\sin(x)$, $\cos(x)$ and $\tan(x)$', 2),
  ('mathematical_methods', 3, 0, 'derivatives of $f(x) \pm g(x)$, $f(x) \times g(x)$, $\frac{f(x)}{g(x)}$ and $(f \circ g)(x)$ where $f$ and $g$ are polynomial functions exponential, circular, logarithmic or power functions and transformations or simple combinations of these functions', 3),
  ('mathematical_methods', 3, 0, 'application of differentiation to graph sketching and identification of key features of graphs, including stationary points and points of inflection, and intervals over which a function is strictly increasing or strictly decreasing', 4),
  ('mathematical_methods', 3, 0, 'identification of local maximum/minimum values over an interval and application to solving optimisation problems in context, including identification of interval endpoint maximum and minimum values', 5),
  ('mathematical_methods', 3, 0, 'anti-derivatives of polynomial functions and functions of the form $f(ax+b)$ where $f$ is $x^n$, for $n \in \mathbb{Q}$, $e^x$, $\sin(x)$, $\cos(x)$ and linear combinations of these', 6),
  ('mathematical_methods', 3, 0, 'informal consideration of the definite integral as a limiting value of a sum involving quantities such as area under a curve and approximation of definite integrals using the trapezium rule', 7),
  ('mathematical_methods', 3, 0, 'anti-differentiation by recognition that $F''(x) = f(x)$ implies $\int f(x)\,dx = F(x) + c$ and informal treatment of the fundamental theorem of calculus, $\int_a^b f(x)\,dx = F(b) - F(a)$', 8),
  ('mathematical_methods', 3, 0, 'properties of anti-derivatives and definite integrals', 9),
  ('mathematical_methods', 3, 0, 'application of integration to problems involving finding a function from a known rate of change given a boundary condition, calculation of the area of a region under a curve and simple cases of areas between curves, average value of a function and other situations.', 10),

  -- AoS 4 — Data analysis, probability and statistics
  ('mathematical_methods', 4, 0, 'random variables, including the concept of a random variable as a real function defined on a sample space and examples of discrete and continuous random variables', 1),
  ('mathematical_methods', 4, 1, 'discrete random variables', 2),
  ('mathematical_methods', 4, 0, 'specification of probability distributions for discrete random variables using graphs, tables and probability mass functions', 3),
  ('mathematical_methods', 4, 0, 'calculation and interpretation of mean, $\mu$, variance, $\sigma^2$, and standard deviation of a discrete random variable and their use', 4),
  ('mathematical_methods', 4, 0, 'Bernoulli trials and the binomial distribution, $\mathrm{Bi}(n, p)$, as an example of a probability distribution for a discrete random variable', 5),
  ('mathematical_methods', 4, 0, 'effect of variation in the value(s) of defining parameters on the graph of a given probability mass function for a discrete random variable', 6),
  ('mathematical_methods', 4, 0, 'calculation of probabilities for specific values of a random variable and intervals defined in terms of a random variable, including conditional probability', 7),
  ('mathematical_methods', 4, 1, 'continuous random variables', 8),
  ('mathematical_methods', 4, 0, 'construction of probability density functions from non-negative functions of a real variable', 9),
  ('mathematical_methods', 4, 0, 'specification of probability distributions for continuous random variables using probability density functions', 10),
  ('mathematical_methods', 4, 0, 'calculation and interpretation of mean, $\mu$, variance, $\sigma^2$, and standard deviation of a continuous random variable and their use', 11),
  ('mathematical_methods', 4, 0, 'standard normal distribution, $N(0, 1)$, and transformed normal distributions, $N(\mu, \sigma^2)$, as examples of a probability distribution for a continuous random variable', 12),
  ('mathematical_methods', 4, 0, 'effect of variation in the value(s) of defining parameters on the graph of a given probability density function for a continuous random variable', 13),
  ('mathematical_methods', 4, 0, 'calculation of probabilities for intervals defined in terms of a random variable, including conditional probability (the cumulative distribution function may be used but is not required)', 14),
  ('mathematical_methods', 4, 1, 'statistical inference, including definition and distribution of sample proportions, simulations and confidence intervals', 15),
  ('mathematical_methods', 4, 0, 'distinction between a population parameter and a sample statistic and the use of the sample statistic to estimate the population parameter', 16),
  ('mathematical_methods', 4, 0, 'simulation of random sampling, for a variety of values of $p$ and a range of sample sizes, to illustrate the distribution of $\hat{P}$ and variations in confidence intervals between samples', 17),
  ('mathematical_methods', 4, 0, 'concept of the sample proportion $\hat{P} = \frac{X}{n}$ as a random variable whose value varies between samples, where $X$ is a binomial random variable which is associated with the number of items that have a particular characteristic and $n$ is the sample size', 18),
  ('mathematical_methods', 4, 0, 'approximate normality of the distribution of $\hat{P}$ for large samples and, for such a situation, the mean $p$ (the population proportion) and standard deviation, $\sqrt{\frac{p(1-p)}{n}}$', 19),
  ('mathematical_methods', 4, 0, 'determination and interpretation of, from a large sample, an approximate confidence interval $\left(\hat{p} - z\sqrt{\frac{\hat{p}(1-\hat{p})}{n}},\ \hat{p} + z\sqrt{\frac{\hat{p}(1-\hat{p})}{n}}\right)$, for a population proportion where $z$ is the appropriate quantile for the standard normal distribution, in particular the 95% confidence interval as an example of such an interval where $z \approx 1.96$ (the term standard error may be used but is not required)', 20)
on conflict (subject, aos, sort_order) do update set text = excluded.text, is_header = excluded.is_header;
