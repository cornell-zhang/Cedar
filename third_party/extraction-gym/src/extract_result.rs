mod extract;

use extract::*;
use egraph_serialize::*;
use ordered_float::NotNan;
use anyhow::Context;
use env_logger;
use std::sync::mpsc;
use std::time::Duration;

pub type Cost = NotNan<f64>;
pub const INFINITY: Cost = unsafe { NotNan::new_unchecked(std::f64::INFINITY) };

fn run_extraction(egraph: &EGraph, extractor_name: &str) -> ExtractionResult {
    let extractor: Box<dyn Extractor> = match extractor_name {
        "faster-greedy-dag" => extract::faster_greedy_dag::FasterGreedyDagExtractor.boxed(),
        "faster-bottom-up" => extract::faster_bottom_up::FasterBottomUpExtractor.boxed(),
        "bottom-up" => extract::bottom_up::BottomUpExtractor.boxed(),
        #[cfg(feature = "ilp-cbc")]
        "ilp-cbc-timeout" => extract::ilp_cbc::CbcExtractorWithTimeout::<10>.boxed(),
        _ => panic!("Unknown extractor: {}", extractor_name),
    };
    extractor.extract(egraph, &egraph.root_eclasses)
}

fn main() {
    env_logger::init();

    let mut args = pico_args::Arguments::from_env();

    let extractor_name: String = args
        .opt_value_from_str("--extractor")
        .unwrap()
        .unwrap_or_else(|| "faster-greedy-dag".into());

    let timeout_secs: u64 = args
        .opt_value_from_str("--timeout")
        .unwrap()
        .unwrap_or(120);

    let filename: String = args.free_from_str().unwrap();

    let rest = args.finish();
    if !rest.is_empty() {
        panic!("Unknown arguments: {:?}", rest);
    }

    let egraph = EGraph::from_json_file(&filename)
        .with_context(|| format!("Failed to parse {filename}"))
        .unwrap();

    let result = if timeout_secs > 0 {
        let (tx, rx) = mpsc::channel();
        let eg = egraph.clone();
        let ext_name = extractor_name.clone();
        std::thread::spawn(move || {
            let r = run_extraction(&eg, &ext_name);
            let _ = tx.send(r);
        });
        match rx.recv_timeout(Duration::from_secs(timeout_secs)) {
            Ok(r) => r,
            Err(_) => {
                eprintln!(
                    "Extraction with '{}' timed out after {}s, falling back to faster-bottom-up",
                    extractor_name, timeout_secs
                );
                run_extraction(&egraph, "faster-bottom-up")
            }
        }
    } else {
        run_extraction(&egraph, &extractor_name)
    };

    result.check(&egraph);

    // Build a map from class_id to node_id for easy lookup
    let class_to_node: std::collections::HashMap<ClassId, NodeId> = result.choices.clone().into_iter().collect();
    
    // Function to recursively build S-expression strings
    fn is_nullary_function(op: &str) -> bool {
        if op.is_empty() { return false; }
        let first = op.as_bytes()[0];
        if first.is_ascii_digit() { return false; }
        if first == b'-' && op.len() > 1 && op.as_bytes()[1].is_ascii_digit() { return false; }
        if first == b'"' { return false; }
        true
    }

    fn count_refs(
        egraph: &EGraph,
        class_to_node: &std::collections::HashMap<ClassId, NodeId>,
        class_id: &ClassId,
        ref_counts: &mut std::collections::HashMap<ClassId, usize>,
    ) {
        let count = ref_counts.entry(class_id.clone()).or_insert(0);
        *count += 1;
        if *count > 1 { return; }
        if let Some(node_id) = class_to_node.get(class_id) {
            for child in &egraph[node_id].children {
                count_refs(egraph, class_to_node, egraph.nid_to_cid(child), ref_counts);
            }
        }
    }

    fn build_sexpr_dag(
        egraph: &EGraph,
        class_to_node: &std::collections::HashMap<ClassId, NodeId>,
        class_id: &ClassId,
        shared: &std::collections::HashSet<ClassId>,
        aliases: &mut std::collections::HashMap<ClassId, String>,
        defs: &mut Vec<String>,
        alias_counter: &mut usize,
    ) -> String {
        if let Some(alias) = aliases.get(class_id) {
            return alias.clone();
        }

        let node_id = match class_to_node.get(class_id) {
            Some(id) => id,
            None => return format!("unknown_{}", class_id),
        };
        
        let node = &egraph[node_id];
        
        if node.children.is_empty() {
            let result = if node.op.starts_with("Var(") && node.op.ends_with(")") {
                let var_name_inner = &node.op[4..node.op.len()-1];
                var_name_inner.trim_matches('"').to_string()
            } else if is_nullary_function(&node.op) {
                format!("({})", node.op)
            } else {
                node.op.clone()
            };
            if shared.contains(class_id) {
                aliases.insert(class_id.clone(), result.clone());
            }
            return result;
        }
        
        let mut child_exprs = Vec::new();
        for child in &node.children {
            let child_class = egraph.nid_to_cid(child);
            let child_expr = build_sexpr_dag(egraph, class_to_node, child_class, shared, aliases, defs, alias_counter);
            child_exprs.push(child_expr);
        }
        
        let op_name = if let Some(paren_pos) = node.op.find('(') {
            &node.op[..paren_pos]
        } else {
            &node.op
        };
        
        let full_expr = format!("({} {})", op_name, child_exprs.join(" "));

        if shared.contains(class_id) {
            let alias = format!("__s{}", *alias_counter);
            *alias_counter += 1;
            defs.push(format!("(let {} {})", alias, full_expr));
            aliases.insert(class_id.clone(), alias.clone());
            return alias;
        }

        full_expr
    }
    
    // Count references to detect shared eclasses
    let mut ref_counts: std::collections::HashMap<ClassId, usize> = std::collections::HashMap::new();
    for root_class in &egraph.root_eclasses {
        count_refs(&egraph, &class_to_node, root_class, &mut ref_counts);
    }
    let shared: std::collections::HashSet<ClassId> = ref_counts.iter()
        .filter(|(cid, &c)| {
            if c <= 1 { return false; }
            // Only alias Op-type eclasses (actual MLIR operations), not types/attrs/vecs
            if let Some(nid) = class_to_node.get(*cid) {
                let node = &egraph[nid];
                !node.children.is_empty() && node.op.contains('_')
            } else {
                false
            }
        })
        .map(|(k, _)| k.clone())
        .collect();

    let mut aliases = std::collections::HashMap::new();
    let mut defs: Vec<String> = Vec::new();
    let mut alias_counter: usize = 0;
    for root_class in &egraph.root_eclasses {
        let sexpr = build_sexpr_dag(&egraph, &class_to_node, root_class, &shared, &mut aliases, &mut defs, &mut alias_counter);
        
        // Skip the RootNode wrapper if present
        let output = if sexpr.starts_with("(RootNode ") && sexpr.ends_with(")") {
            &sexpr[10..sexpr.len()-1]
        } else {
            &sexpr
        };

        // Emit shared sub-expression definitions before the root expression
        for def in &defs {
            println!("{}", def);
        }
        defs.clear();

        println!("{}", output);
    }
}