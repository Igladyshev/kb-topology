# Topology Methods Applied to OKF Knowledge Base

This document explains the mathematical basis of the topology-driven knowledge-graph approach used in this project. It is written in the same language as mathematical topology and graph theory, but translated into operational rules for OKF concepts, documents, metadata, and repaired references.

## 1. Formal model

Let $V$ be the set of concepts in the knowledge base, and let

$$
E \subseteq \binom{V}{2}
$$

be the set of known relations among them. Then the knowledge base is the undirected graph

$$
G = (V, E).
$$

In the implementation, each concept is represented by a `Concept`, and each relation is stored as a typed edge. The graph is a finite discrete structure that serves as a computational approximation of a topological space in which local neighborhoods, connectivity, and discontinuities can be analyzed.

This is not merely a visualization of links. The graph is the mathematical object used to reason about connection, discontinuity, and repair.

## 2. Locality, neighborhoods, and the notion of semantic proximity

In topology, a neighborhood of a point is any set containing an open neighborhood around that point.[^r1] In the current project, we approximate this by graph neighborhoods:

$$
N_k(v) = \{u \in V : d_G(u,v) \le k\},
$$

where $d_G(u,v)$ denotes the shortest-path distance between $u$ and $v$ in the knowledge graph.

This gives a mathematically grounded notion of local context: the concepts near a node are its local semantic surroundings. The code implements this in `neighborhood(concept_id, depth)`.

### Example A: two concept documents

Consider two concept documents:

$$
d_1 = \text{Payments database schema}, \qquad d_2 = \text{Payments reconciliation pipeline}.
$$

Suppose their tags are:

$$
T(d_1) = \{\text{payments}, \text{database}, \text{schema}\},
$$

$$
T(d_2) = \{\text{payments}, \text{pipeline}, \text{reconciliation}\}.
$$

Then

$$
T(d_1) \cap T(d_2) = \{\text{payments}\},
$$

and their bundle context and lexical signatures also overlap. Thus the graph can detect that the two nodes are not random but belong to a common semantic region. A link is recognized as plausible when the continuity signal exceeds a threshold.

This is precisely the finite-graph analogue of a topology rule: a small local perturbation should not create a large semantic jump.

### Example B: two documents that should not be linked

Now consider:

$$
d_3 = \text{Customer support playbook}, \qquad d_4 = \text{Kafka partition rebalancing}.
$$

Then

$$
T(d_3) \cap T(d_4) \approx \varnothing,
$$

and their neighborhoods do not reinforce one another. In the scoring function, the continuity signal is too low. So the model rejects a link even though the two documents may both exist in the same large KB.

Formally, this is the rejection of a candidate edge

$$
\{d_3, d_4\}
$$

when the associated continuity score falls below the accepted threshold.

## 3. Continuity-inspired scoring

Topology is concerned with continuity, that is, the preservation of local structure under mapping. In the project, we implement the practical analog: a candidate link should preserve local semantic continuity.

For candidate concepts $a$ and $b$, the score is

$$
S(a,b)=0.45\,J(T_a,T_b)+0.35\,J(L_a,L_b)+0.20\,I_{ab},
$$

where $I_{ab}$ is the bundle indicator function:

$$
I_{ab} =
\begin{cases}
1, & \text{if } \mathrm{bundle}(a)=\mathrm{bundle}(b), \\
0, & \text{otherwise}.
\end{cases}
$$

Here $J$ is the Jaccard similarity, $T_a$ and $T_b$ are the tag sets of $a$ and $b$, $L_a$ and $L_b$ are the lexical signatures of $a$ and $b$, and $I_{ab}$ is the same-bundle indicator.

The Jaccard similarity is

$$
J(A,B)=\frac{|A\cap B|}{|A\cup B|}.
$$

The implementation appears in `TopologyKnowledgeGraph._score_candidate()` and `suggest_cross_links()`.

### Proposition 1 (local continuity principle)

If two documents have highly overlapping tags and lexical context, then their graph distance under a semantic similarity model is expected to be small, and the edge is a plausible continuity-preserving relation.

### Proof sketch

In a finite graph, local neighborhoods are represented by short paths and shared vocabulary. When two nodes share a high proportion of local semantic content, their neighborhoods overlap. Since the graph’s local structure reflects this overlap, the candidate pair behaves like a continuation of the same conceptual region rather than a distant semantic jump.

This is an engineering approximation of continuity, but it is mathematically consistent with how locality is represented in graphs.

## 4. Three-document example: connected semantic chains

Now consider three concept documents:

- $d_1$: "Orders database"
- $d_2$: "Orders topic"
- $d_3$: "Orders pipeline"

Suppose their weights and tags indicate:

- $d_1$ and $d_2$ share `orders` and `database/topic` semantics,
- $d_2$ and $d_3$ share `orders` and `streaming/integration` semantics,
- $d_1$ and $d_3$ share indirect but relevant metadata.

Then the graph contains a short path:

$$
d_1 \sim d_2 \sim d_3,
$$

where each adjacency indicates a local semantic relation. This is not arbitrary. The trio forms a semantic corridor: each node is locally related to its predecessor and successor. The path is a discrete analogue of a connected region.

By contrast, if the documents are:

- `orders database`
- `customer support playbook`
- `network load balancer`

then there is no meaningful chain, because the pairwise overlap is too weak. The three-document pattern illustrates the same principle as the two-document case, but it reveals a stronger topological structure: a local chain of concepts with a shared narrative or data-flow region.

## 5. Connected components and the structure of the KB

A topological space is connected if it cannot be partitioned into two nonempty disjoint open sets.[^r1] In graph theory, the corresponding property is path connectivity.

The implementation computes connected components in `_connected_components()`. These components define knowledge islands: regions of concepts that are strongly connected through paths of relations.

If the KB has many components, then the corpus has fragmented thematic regions; if one large component dominates, the corpus is better integrated.

### Example: three conceptual islands

Suppose we have:

- component $C_1$: payments, ledger, settlement, reconciliation
- component $C_2$: customer profiles, identity, permissions
- component $C_3$: infra, deployment, cluster health

These components may all be part of the same enterprise KB, but they are different semantic islands. The topology layer flags them as distinct connected components. This is useful because it identifies where cross-links or bridges are needed.

## 6. Articulation points and bridge documents

In topology, a cut point is a point whose removal disconnects a connected space.[^r2] In graph theory, the analog is an articulation point or articulation vertex.

The project computes articulation points using Tarjan’s low-link algorithm in `_articulation_points()`.

These are the documents whose absence most sharply affects connectivity. In a KB, such nodes are often critical: they are semantic bridges between domain subgraphs.

### Example

If a document `Payments API` connects `billing`, `ledger`, and `settlement`, then removing it can split a connected region into multiple components. That node is an articulation point. In practical terms, it is a bridge concept whose structure should be reviewed carefully.

## 7. Jaccard distance as a metric foundation

The implementation uses Jaccard similarity in `_jaccard()`. The associated distance is:

$$
D_J(A,B) = 1 - \frac{|A \cap B|}{|A \cup B|}
$$

for finite sets $A$ and $B$. This is a proper metric on finite sets.[^r7]

This gives a mathematically valid measure of how distant two semantic signatures are. In a KB context, it helps decide whether concept neighborhoods are nearby or too far apart to support a relation.

## 8. Generalization to n documents

The previous examples illustrate the two-document and three-document cases. The general case is the finite graph model:

$$
G = (V,E), \quad |V| = n
$$

The task is to infer a set of relations that are locally consistent with the observed semantics. This interpretation is operationalized as follows:

1. Each concept is a node.
2. Each relation is an edge with a semantic type.
3. Local topology is defined by neighborhoods and short paths.
4. A candidate link is valid if it does not create a large discontinuity in local semantic structure.
5. The graph is repaired as a discrete topological system, not as a purely lexical list.

This general mechanism extends naturally to large corpora. For large $n$, the graph structure remains finite and analyzable. Components, articulation points, local neighborhoods, and Jaccard-based relation scoring scale to the whole system.

In this sense, the KB becomes a finite topological network over which navigation, validation, and repair are all defined.

## 9. Reference repair as edge completion under continuity constraints

The project also includes a repair mechanism for partial or broken references. This is a natural topological operation: the graph is locally incomplete, and the algorithm attempts to complete the missing edge using neighboring evidence.

The logic appears in `suggest_reference_repairs()` and `_score_reference_candidate()`.

The algorithm works as follows:

1. Extract mention-like strings from the document text.
2. Compare them to known concept IDs and lexical signatures.
3. Score candidate targets using semantic continuity and local similarity.
4. If the score is above threshold and the relation is missing, add a repaired edge.

This is equivalent to inferencing a missing edge in a graph under local continuity constraints. The operation is not arbitrary; it is a constrained completion problem on a topology-defined neighborhood structure.

## 10. Why this is real topology in practice

The project uses genuine topology and graph-theoretic ideas:

- neighborhoods and locality,[^r1]
- connectedness and components,[^r1][^r5]
- articulation points as structural cut vertices,[^r2][^r6]
- metric-based similarity via Jaccard distance,[^r7]
- graph-theoretic modeling of finite semantic spaces.

What is engineering approximation is the mapping from abstract semantics to token-based features and tags. The formal topology is retained at the graph level; the semantics are represented computationally, not as a fully formal topological space in the mathematical sense.

Hence this is not a metaphor. It is an applied discrete-topology method adapted to KB navigation, validation, and repair.

## 11. How to read the outputs in this repo

Run:

```bash
uv run main.py
```

You will see:

1. top cross-link suggestions,
2. graph diagnostics,
3. reference repair suggestions.

Run:

```bash
uv run main.py --pipeline --output-dir artifacts
```

This writes the original KB and the repaired KB, plus Mermaid diagrams and repair metadata. These outputs are the operational manifestation of the theoretical model above.

[^r1]: James R. Munkres, *Topology* (2nd ed.), Prentice Hall, 2000.
[^r2]: Stephen Willard, *General Topology*, Dover, 2004.
[^r5]: Reinhard Diestel, *Graph Theory* (5th ed.), Springer, 2017.
[^r6]: Robert E. Tarjan, "Depth-first search and linear graph algorithms," *SIAM Journal on Computing* 1(2), 1972.
[^r7]: Sven Kosub, "A note on the triangle inequality for the Jaccard distance," *Pattern Recognition Letters* 120, 2019.
