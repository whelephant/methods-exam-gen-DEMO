-- VCE Specialist Mathematics — Units 3 & 4 — verbatim AoS dot points.
-- Source: VCE Mathematics Study Design 2023 (Updated v1.1), pages 111–117.
-- Math reconstructed via Claude Sonnet 4.6 vision OCR of LibreOffice-rendered PDF;
-- every $...$ block KaTeX-validated for balanced braces and parens.
-- Schema lives in `migrations/002_study_design.sql` + `migrations/005_add_subject.sql`.
-- Idempotent: safe to re-run.

-- ─── AREAS ───────────────────────────────────────────────────────────

insert into study_areas (subject, aos, title, intro) values
  ('specialist_mathematics', 1, 'Discrete mathematics',
   'In this area of study students cover the development of mathematical argument and proof. This includes conjectures, connectives, quantifiers, examples and counter-examples, and proof techniques including mathematical induction. Proofs will involve concepts from topics such as: divisibility, inequalities, graph theory, combinatorics, sequences and series including partial sums and partial products and related notations, complex numbers, matrices, vectors and calculus. The concepts, skills and processes from this area of study are to be applied in the other areas of study.'),
  ('specialist_mathematics', 2, 'Functions, relations and graphs',
   'In this area of study students cover rational functions and other simple quotient functions, curve sketching of these functions and relations, and the analysis of key features of their graphs including intercepts, asymptotic behaviour and the nature and location of stationary points and points of inflection and symmetry.'),
  ('specialist_mathematics', 3, 'Algebra, number and structure',
   'In this area of study students cover the algebra of complex numbers, including polar form, factorisation of polynomial functions over the complex field and an informal treatment of the fundamental theorem of algebra.'),
  ('specialist_mathematics', 4, 'Calculus',
   'In this area of study students cover the advanced calculus techniques for analytical and numerical differentiation and integration of a broad range of functions, and combinations of functions; and their application in a variety of theoretical and practical situations, including curve sketching, evaluation of arc length, area and volume, differential equations and kinematics, and modelling with differential equations drawing from a variety of fields such as biology, economics and science.'),
  ('specialist_mathematics', 5, 'Space and measurement',
   'In this area of study students cover the arithmetic and algebra of vectors; linear dependence and independence of a set of vectors; proof of geometric results using vectors; vector representation of curves in the plane and their parametric and Cartesian equations; vector kinematics in one, two and three dimensions; vector, parametric and Cartesian equations of lines and planes.'),
  ('specialist_mathematics', 6, 'Data analysis, probability and statistics',
   'In this area of study students cover the study of linear combinations of random variables and introductory statistical inference with respect to the mean of a single population, the determination of confidence intervals, and hypothesis testing for the mean using the distribution of sample means.')
on conflict (subject, aos) do update set title = excluded.title, intro = excluded.intro;

-- ─── POINTS ──────────────────────────────────────────────────────────
-- Sort_order is the 1-based position within each AoS. is_header=true marks
-- sub-section headings that group related dot points but are NOT tagable.

-- Delete existing Specialist points before re-inserting (the (subject, aos, sort_order) PK
-- would otherwise prevent removing a previously-seeded bullet that has since been merged).
--
-- foreign_keys = off around the delete/insert because `question_tags` rows reference
-- (subject, aos, sort_order) — the delete would otherwise fail once tags exist.
-- The matching INSERT immediately below restores all referenced rows, so FK integrity
-- is maintained at end-of-script. Re-enabled at the bottom.
pragma foreign_keys = off;

delete from study_points where subject = 'specialist_mathematics';

insert into study_points (subject, aos, is_header, text, sort_order) values
  -- AoS 1 — Discrete mathematics
  ('specialist_mathematics', 1, true, 'Logic and proof', 1),
  ('specialist_mathematics', 1, false, 'conjecture – making a statement to be proved or disproved', 2),
  ('specialist_mathematics', 1, false, 'implications, equivalences and if and only if statements (necessary and sufficient conditions)', 3),
  ('specialist_mathematics', 1, false, 'natural deduction and proof techniques: direct proofs using a sequence of direct implications, proof by cases, proof by contradiction, and proof by contrapositive', 4),
  ('specialist_mathematics', 1, false, 'quantifiers ''for all'' and ''there exists'', examples and counter-examples', 5),
  ('specialist_mathematics', 1, false, 'proof by mathematical induction.', 6),
  -- AoS 2 — Functions, relations and graphs
  ('specialist_mathematics', 2, false, 'rational functions and the expression of rational functions of low degree as sums of partial fractions', 1),
  ('specialist_mathematics', 2, false, 'graphs of rational functions of low degree, their asymptotic behaviour, and the nature and location of stationary points and points of inflection', 2),
  ('specialist_mathematics', 2, false, 'graphs of simple quotient functions, their asymptotic behaviour, and the nature and location of stationary points and points of inflection.', 3),
  -- AoS 3 — Algebra, number and structure
  ('specialist_mathematics', 3, true, 'Complex numbers', 1),
  ('specialist_mathematics', 3, false, 'De Moivre''s theorem, proof for integral powers, powers and roots of complex numbers in polar form, and their geometric representation and interpretation', 2),
  ('specialist_mathematics', 3, false, 'the $n$th roots of unity and other complex numbers and their location in the complex plane', 3),
  ('specialist_mathematics', 3, false, 'factors over $C$ of polynomials; and introduction to the fundamental theorem of algebra, including its application to factorisation of polynomial functions of a single variable over $C$, for example, $z^{8} + 1$, $z^{2} - i$ or $z^{3} - (2 - i)z^{2} + z - 2 + i$', 4),
  ('specialist_mathematics', 3, false, 'solution over $C$ of polynomial equations by completing the square, use of the quadratic factorisation and the conjugate root theorem.', 5),
  -- AoS 4 — Calculus
  ('specialist_mathematics', 4, true, 'Differential calculus and integral calculus', 1),
  ('specialist_mathematics', 4, false, 'the relationship between the graph of a function and the graphs of its anti-derivative functions', 2),
  ('specialist_mathematics', 4, false, 'derivatives of inverse circular functions', 3),
  ('specialist_mathematics', 4, false, 'second derivatives, use of notations $f''''(x)$ and $\frac{d^{2}y}{dx^{2}}$, and their application to the analysis of graphs of functions, including points of inflection and concavity', 4),
  ('specialist_mathematics', 4, false, 'applications of chain rule to related rates of change and implicit differentiation; for example, implicit differentiation of the relations $x^{2} + y^{2} = 9$, $3x y^{2} = x + y$ and $x \sin(y) + x^{2}\cos(y) = 1$', 5),
  ('specialist_mathematics', 4, true, 'techniques of anti-differentiation and for the evaluation of definite integrals', 6),
  ('specialist_mathematics', 4, false, 'anti-differentiation of $\frac{1}{x}$ to obtain $\log_{e}|x|$', 7),
  ('specialist_mathematics', 4, false, 'anti-differentiation of $\frac{1}{\sqrt{a^{2} - x^{2}}}$ and $\frac{a}{a^{2} + x^{2}}$ for $a > 0$ by recognition that they are derivatives of corresponding inverse circular functions', 8),
  ('specialist_mathematics', 4, false, 'use of the substitution $u = g(x)$ to anti-differentiate expressions', 9),
  ('specialist_mathematics', 4, false, 'use of the trigonometric identities $\sin^{2}(ax) = \frac{1}{2}(1 - \cos(2ax))$ and $\cos^{2}(ax) = \frac{1}{2}(1 + \cos(2ax))$ in anti-differentiation techniques', 10),
  ('specialist_mathematics', 4, false, 'anti-differentiation using partial fractions of rational functions', 11),
  ('specialist_mathematics', 4, false, 'integration by parts', 12),
  ('specialist_mathematics', 4, false, 'numerical and symbolic integration using technology', 13),
  ('specialist_mathematics', 4, false, 'application of integration, areas of regions bounded by curves, arc lengths for parametrically determined curves, surface area of solids of revolution, volumes of solids of revolution of a region about either coordinate axis.', 14),
  ('specialist_mathematics', 4, true, 'Differential equations', 15),
  ('specialist_mathematics', 4, false, 'formulation of differential equations from contexts in, for example, chemistry, biology and economics, in situations where rates are involved (including some differential equations whose analytic solutions are not required, but can be solved numerically using technology)', 16),
  ('specialist_mathematics', 4, false, 'the logistic differential equation', 17),
  ('specialist_mathematics', 4, false, 'verification of solutions of differential equations and their representation using direction (slope) fields', 18),
  ('specialist_mathematics', 4, false, 'solution of simple differential equations of the form $\frac{dy}{dx} = f(x)$, $\frac{dy}{dx} = g(y)$ and in general differential equations of the form $\frac{dy}{dx} = f(x)g(y)$ using separation of variables and differential equations of the form $\frac{d^{2}y}{dx^{2}} = f(x)$', 19),
  ('specialist_mathematics', 4, false, 'numerical solution by Euler''s method (first order approximation).', 20),
  ('specialist_mathematics', 4, true, 'Kinematics: rectilinear motion', 21),
  ('specialist_mathematics', 4, false, 'use of velocity–time graphs to describe and analyse rectilinear motion', 22),
  ('specialist_mathematics', 4, false, 'application of differentiation, anti-differentiation and solution of differential equations to rectilinear motion of a single particle, including the different derivative forms for acceleration $a = \frac{d^{2}x}{dt^{2}} = \frac{dv}{dt} = v\frac{dv}{dx} = \frac{d}{dx}\left(\frac{1}{2}v^{2}\right)$.', 23),
  -- AoS 5 — Space and measurement
  ('specialist_mathematics', 5, true, 'Vectors', 1),
  ('specialist_mathematics', 5, false, 'addition and subtraction of vectors and their multiplication by a scalar, position vectors', 2),
  ('specialist_mathematics', 5, false, 'linear dependence and independence of a set of vectors and geometric interpretation', 3),
  ('specialist_mathematics', 5, false, 'magnitude of a vector, unit vector, the orthogonal unit vectors $\underset{\sim}{i}$, $\underset{\sim}{j}$ and $\underset{\sim}{k}$', 4),
  ('specialist_mathematics', 5, false, 'resolution of a vector into rectangular components', 5),
  ('specialist_mathematics', 5, false, 'scalar (dot) product of two vectors, deduction of dot product for the $\underset{\sim}{i}$, $\underset{\sim}{j}$ and $\underset{\sim}{k}$ vector system and its use to find scalar resolute and vector resolute', 6),
  ('specialist_mathematics', 5, false, 'vector (cross) product of two vectors in three dimensions, including the determinant form', 7),
  ('specialist_mathematics', 5, false, 'parallel and perpendicular vectors', 8),
  ('specialist_mathematics', 5, false, 'vector proofs of simple geometric results, such as ''the diagonals of a rhombus are perpendicular'', ''the medians of a triangle are concurrent'' and ''the angle subtended by a diameter in a circle is a right angle''.', 9),
  ('specialist_mathematics', 5, true, 'Vector and Cartesian equations', 10),
  ('specialist_mathematics', 5, false, 'vector equations and parametric equations of curves in two or three dimensions involving a parameter (and the corresponding Cartesian equation in the two-dimensional case)', 11),
  ('specialist_mathematics', 5, false, 'vector equation of a straight line, given the position of two points, or equivalent information, in both two and three dimensions', 12),
  ('specialist_mathematics', 5, false, 'vector cross product, normal to a plane and vector, parametric and Cartesian equations of a plane.', 13),
  ('specialist_mathematics', 5, true, 'Vector calculus', 14),
  ('specialist_mathematics', 5, false, 'position vector as a function of time and sketching the corresponding path given the function, including circles, ellipses and hyperbolas in Cartesian or parametric forms', 15),
  ('specialist_mathematics', 5, false, 'the positions of two particles each described as a vector function of time, and whether their paths cross or if the particles meet', 16),
  ('specialist_mathematics', 5, false, 'differentiation and anti-differentiation of a vector function with respect to time and applying vector calculus to motion in a plane and in three dimensions.', 17),
  -- AoS 6 — Data analysis, probability and statistics
  ('specialist_mathematics', 6, true, 'Distribution of linear combinations of random variables', 1),
  ('specialist_mathematics', 6, false, 'for $n$ independent identically distributed random variables $X_{1}$, $X_{2} \ldots X_{n}$ each with mean $\mu$ and variance $\sigma^{2}$: $E(X_{1} + X_{2} + \ldots + X_{n}) = n\mu$ and $Var(X_{1} + X_{2} + \ldots + X_{n}) = n\sigma^{2}$', 2),
  ('specialist_mathematics', 6, false, 'for $n$ independent random variables $X_{1}$, $X_{2} \ldots X_{n}$ and real numbers $a_{1}$, $a_{2} \ldots a_{n}$: $E(a_{1}X_{1} + a_{2}X_{2} + \ldots + a_{n}X_{n}) = a_{1}E(X_{1}) + a_{2}E(X_{2}) + \ldots + a_{n}E(X_{n})$ and $Var(a_{1}X_{1} + a_{2}X_{2} + \ldots + a_{n}X_{n}) = a_{1}^{2}Var(X_{1}) + a_{2}^{2}Var(X_{2}) + \ldots + a_{n}^{2}Var(X_{n})$', 3),
  ('specialist_mathematics', 6, false, 'for $n$ normally distributed independent random variables $X_{1}$, $X_{2} \ldots X_{n}$ and real numbers $a_{1}$, $a_{2} \ldots a_{n}$ the random variable $a_{1}X_{1} + a_{2}X_{2} + \ldots + a_{n}X_{n}$ is also normally distributed.', 4),
  ('specialist_mathematics', 6, true, 'Distribution of the sample mean', 5),
  ('specialist_mathematics', 6, false, 'the concept of the sample mean $\overline{X}$ as a random variable whose value varies between samples where $X$ is a random variable with mean $\mu$ and the standard deviation $\sigma$', 6),
  ('specialist_mathematics', 6, false, 'simulation of repeated random sampling, from a variety of distributions and a range of sample sizes, to illustrate properties of the distribution of $\overline{X}$ across samples of a fixed size $n$ including its mean $\mu$ its standard deviation $\frac{\sigma}{\sqrt{n}}$ (where $\mu$ and $\sigma$ are the mean and standard deviation of $X$ respectively) and its approximate normality if $n$ is large.', 7),
  ('specialist_mathematics', 6, true, 'Confidence intervals for the population mean', 8),
  ('specialist_mathematics', 6, false, 'determination of confidence intervals for means and the use of simulation to illustrate variations in confidence intervals between samples and to show that the likelihood of a confidence interval containing $\mu$ depends on the level of confidence chosen in the determination of the interval', 9),
  ('specialist_mathematics', 6, false, 'construction of an approximate confidence interval, $\left(\bar{x} - z\frac{\sigma}{\sqrt{n}},\ \bar{x} + z\frac{\sigma}{\sqrt{n}}\right)$ where $\sigma$ is the population standard deviation and $z$ is the appropriate quantile for the standard normal distribution or construction of an approximate confidence interval $\left(\bar{x} - z\frac{s}{\sqrt{n}},\ \bar{x} + z\frac{s}{\sqrt{n}}\right)$ where $s$ is the sample standard deviation and $z$ is the appropriate quantile for the standard normal distribution, and $n$ is large ($n \geq 30$ in many practical contexts).', 10),
  ('specialist_mathematics', 6, true, 'Hypothesis testing for a population mean with a sample drawn from a normal distribution of known variance, or for a large sample', 11),
  ('specialist_mathematics', 6, false, 'concepts of null hypothesis, $H_{0}$, and alternative hypotheses, $H_{1}$, test statistic', 12),
  ('specialist_mathematics', 6, false, 'level of significance and $p$-value', 13),
  ('specialist_mathematics', 6, false, 'formulation of hypotheses and making a decision concerning a population mean based on: a random sample from a normal population of known variance; a large random sample from any population', 14),
  ('specialist_mathematics', 6, false, '1-tail and 2-tail tests', 15),
  ('specialist_mathematics', 6, false, 'interpretation of the results of a hypothesis test in the context of the problem', 16),
  ('specialist_mathematics', 6, false, 'hypothesis test, relating the formulation, conduct, errors and results in terms of conditional probability.', 17)
on conflict (subject, aos, sort_order) do update set
  is_header = excluded.is_header,
  text      = excluded.text;

pragma foreign_keys = on;
