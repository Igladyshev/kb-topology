from argparse import ArgumentParser
import json
from pathlib import Path
from typing import Any

from kb_topology.graph import TopologyKnowledgeGraph


def build_synthetic_okf_graph() -> TopologyKnowledgeGraph:
    graph = TopologyKnowledgeGraph()

    bundles = ("databases", "kafka-topics", "pipelines", "services")
    domains = ("orders", "inventory", "payments", "customer")

    concept_ids: dict[tuple[str, str], list[str]] = {(bundle, domain): [] for bundle in bundles for domain in domains}
    text_overrides = {
        "ser-orders-2": "Depends on kaf-orders-2 and dat-paymnts-1 for settlement decisions.",
        "pip-inventory-3": "Ingests kaf-inventry-2 snapshots before writing state to inventory marts.",
        "ser-customer-2": "Publishes lifecycle updates to kaf-custmer-1 when profile status changes.",
        "pip-payments-3": "Joins dat-paymnts-2 with kaf-payments-2 to compute reconciliation metrics.",
    }

    for bundle in bundles:
        for domain in domains:
            for index in range(1, 4):
                concept_id = f"{bundle[:3]}-{domain}-{index}"
                title = f"{domain.title()} {bundle.replace('-', ' ')} concept {index}"
                tags = {bundle.rstrip("s"), domain, f"v{index}"}
                text = (
                    f"{domain.title()} knowledge object in {bundle}. "
                    f"Contains operational semantics, ownership, and integration patterns."
                )
                if concept_id in text_overrides:
                    text = f"{text} {text_overrides[concept_id]}"
                graph.add_concept(
                    concept_id=concept_id,
                    title=title,
                    bundle=bundle,
                    tags=tags,
                    text=text,
                )
                concept_ids[(bundle, domain)].append(concept_id)

    for bundle in bundles:
        for domain in domains:
            first, second, third = concept_ids[(bundle, domain)]
            graph.add_relation(first, second, relation_type="related")
            graph.add_relation(second, third, relation_type="related")

    for domain in domains:
        graph.add_relation(concept_ids[("databases", domain)][0], concept_ids[("kafka-topics", domain)][0], relation_type="publishes")
        graph.add_relation(concept_ids[("kafka-topics", domain)][1], concept_ids[("pipelines", domain)][0], relation_type="consumed-by")
        graph.add_relation(concept_ids[("pipelines", domain)][1], concept_ids[("services", domain)][0], relation_type="feeds")

    graph.add_relation(concept_ids[("pipelines", "orders")][2], concept_ids[("databases", "payments")][0], relation_type="updates")
    graph.add_relation(concept_ids[("services", "inventory")][2], concept_ids[("kafka-topics", "orders")][2], relation_type="emits")
    graph.add_relation(concept_ids[("services", "customer")][2], concept_ids[("databases", "orders")][1], relation_type="reads")
    return graph


def as_markdown_mermaid(mermaid_script: str) -> str:
    return f"```mermaid\n{mermaid_script}\n```"


def kb_payload_from_graph(graph: TopologyKnowledgeGraph) -> dict[str, Any]:
    return {
        "concepts": [
            {
                "concept_id": concept.concept_id,
                "title": concept.title,
                "bundle": concept.bundle,
                "tags": sorted(concept.tags),
                "text": concept.text,
            }
            for concept in graph.concepts()
        ],
        "relations": [
            {
                "source_id": relation.source_id,
                "target_id": relation.target_id,
                "relation_type": relation.relation_type,
                "provenance": relation.provenance,
                "confidence": relation.confidence,
                "status": relation.status,
                **({"metadata": relation.metadata} if relation.metadata else {}),
            }
            for relation in graph.relation_records()
        ],
    }


def graph_from_kb_payload(payload: dict[str, Any]) -> TopologyKnowledgeGraph:
    graph = TopologyKnowledgeGraph()
    for concept in payload.get("concepts", []):
        graph.add_concept(
            concept_id=concept["concept_id"],
            title=concept["title"],
            bundle=concept["bundle"],
            tags=concept.get("tags", []),
            text=concept.get("text", ""),
        )
    for relation in payload.get("relations", []):
        graph.add_relation(
            source_id=relation["source_id"],
            target_id=relation["target_id"],
            relation_type=relation.get("relation_type", "related"),
            provenance=relation.get("provenance", "manual"),
            confidence=relation.get("confidence", 1.0),
            status=relation.get("status", "active"),
            metadata=relation.get("metadata", {}),
        )
    return graph


def write_json(path: Path, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args() -> Any:
    parser = ArgumentParser(description="Topology KB demo and Mermaid export")
    parser.add_argument(
        "--format",
        choices=("text", "mermaid", "markdown"),
        default="text",
        help="Output format. text=diagnostics, mermaid=.mmd script only, markdown=```mermaid``` block.",
    )
    parser.add_argument("--output", type=Path, help="Optional output file path")
    parser.add_argument(
        "--pipeline",
        action="store_true",
        help="Run persisted KB repair pipeline and write original/fixed KB + Mermaid files.",
    )
    parser.add_argument(
        "--input-kb",
        type=Path,
        help="Optional input KB JSON file with concepts/relations. If omitted, synthetic broken KB is generated.",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts"), help="Pipeline output directory.")
    parser.add_argument(
        "--repair-confidence",
        type=float,
        default=0.5,
        help="Minimum confidence for persisting reference-based repair links.",
    )
    parser.add_argument(
        "--include-ambiguous-repairs",
        action="store_true",
        help="Persist ambiguous repairs too. Default keeps only unambiguous repair links.",
    )
    return parser.parse_args()


def run_persisted_repair_pipeline(args: Any) -> None:
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.input_kb:
        payload = json.loads(args.input_kb.read_text(encoding="utf-8"))
        original_graph = graph_from_kb_payload(payload)
    else:
        original_graph = build_synthetic_okf_graph()

    repairs = original_graph.suggest_reference_repairs(top_k=5000, min_confidence=args.repair_confidence)
    selected_repairs = [
        repair
        for repair in repairs
        if repair.confidence >= args.repair_confidence and (args.include_ambiguous_repairs or not repair.ambiguous)
    ]

    fixed_graph = original_graph.clone()
    for repair in selected_repairs:
        if not fixed_graph.neighborhood(repair.source_id, depth=1) >= {repair.target_id}:
            fixed_graph.add_relation(
                repair.source_id,
                repair.target_id,
                relation_type="repaired-reference",
                provenance="topology-repair",
                confidence=repair.confidence,
                status="proposed" if repair.ambiguous else "accepted",
                metadata={
                    "mention": repair.mention,
                    "reason": repair.reason,
                    "ambiguous": repair.ambiguous,
                },
            )

    write_json(output_dir / "kb_original.json", kb_payload_from_graph(original_graph))
    write_json(output_dir / "kb_fixed.json", kb_payload_from_graph(fixed_graph))
    write_json(
        output_dir / "repair_suggestions.json",
        [
            {
                "source_id": repair.source_id,
                "target_id": repair.target_id,
                "mention": repair.mention,
                "confidence": round(repair.confidence, 6),
                "ambiguous": repair.ambiguous,
                "reason": repair.reason,
            }
            for repair in repairs
        ],
    )

    (output_dir / "kb_original.mmd").write_text(original_graph.to_mermaid() + "\n", encoding="utf-8")
    (output_dir / "kb_fixed.mmd").write_text(fixed_graph.to_mermaid() + "\n", encoding="utf-8")

    print(f"Pipeline completed in {output_dir}")
    print(f"- Original KB: {output_dir / 'kb_original.json'}")
    print(f"- Fixed KB: {output_dir / 'kb_fixed.json'}")
    print(f"- Repair suggestions: {output_dir / 'repair_suggestions.json'}")
    print(f"- Mermaid (original): {output_dir / 'kb_original.mmd'}")
    print(f"- Mermaid (fixed): {output_dir / 'kb_fixed.mmd'}")
    print(f"- Applied repair links: {len(selected_repairs)}")


if __name__ == "__main__":
    args = parse_args()
    if args.pipeline:
        run_persisted_repair_pipeline(args)
    else:
        graph = build_synthetic_okf_graph()
        mermaid = graph.to_mermaid()

        if args.format == "mermaid":
            rendered = mermaid
        elif args.format == "markdown":
            rendered = as_markdown_mermaid(mermaid)
        else:
            suggestions = graph.suggest_cross_links(top_k=15, min_score=0.22)
            repairs = graph.suggest_reference_repairs(top_k=15, min_confidence=0.50)
            diagnostics = graph.topology_diagnostics()

            print(f"Synthetic KB loaded: {graph.concept_count} concepts, {graph.relation_count} relations")
            print("\nTop cross-link suggestions:")
            for suggestion in suggestions:
                print(
                    f"- {suggestion.source_id} -> {suggestion.target_id} "
                    f"(score={suggestion.score:.3f}, reason={suggestion.reason})"
                )

            print("\nTopology diagnostics:")
            print(f"- Connected components: {diagnostics.connected_components_count}")
            print(f"- Orphan concepts: {sorted(diagnostics.orphan_concepts)}")
            print(f"- Articulation points: {sorted(diagnostics.articulation_points)}")
            print(f"- Sparse concepts: {sorted(diagnostics.sparse_concepts)}")
            print("\nReference repair suggestions:")
            for repair in repairs:
                print(
                    f'- {repair.source_id} -> {repair.target_id} '
                    f'(mention="{repair.mention}", confidence={repair.confidence:.3f}, reason={repair.reason})'
                )
            print("\nUse --format mermaid or --format markdown to export the graph.")
            rendered = ""

        if args.output and rendered:
            args.output.write_text(rendered + "\n", encoding="utf-8")
        elif rendered:
            print(rendered)
