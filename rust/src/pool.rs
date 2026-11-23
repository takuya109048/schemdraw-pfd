//! Run independent builds on several threads.  The builds are
//! deterministic, so the results are the same however many threads work
//! on them -- only the wall time differs.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::thread;

/// Number of worker threads for ``jobs`` (None: one per CPU).
pub fn workers(jobs: Option<usize>, tasks: usize) -> usize {
    let cpus = thread::available_parallelism().map(|n| n.get()).unwrap_or(1);
    jobs.unwrap_or(cpus).max(1).min(tasks.max(1))
}

/// ``f(task)`` for every task, in order.
pub fn pool_map<T: Sync, R: Send>(tasks: &[T], jobs: Option<usize>, f: impl Fn(&T) -> R + Sync) -> Vec<R> {
    let n = workers(jobs, tasks.len());
    if n <= 1 || tasks.len() <= 1 {
        return tasks.iter().map(f).collect();
    }
    let next = AtomicUsize::new(0);
    let mut slots: Vec<Option<R>> = (0..tasks.len()).map(|_| None).collect();
    let results: Vec<Vec<(usize, R)>> = thread::scope(|s| {
        let handles: Vec<_> = (0..n)
            .map(|_| {
                s.spawn(|| {
                    let mut out = Vec::new();
                    loop {
                        let k = next.fetch_add(1, Ordering::Relaxed);
                        if k >= tasks.len() {
                            break;
                        }
                        out.push((k, f(&tasks[k])));
                    }
                    out
                })
            })
            .collect();
        handles
            .into_iter()
            .map(|h| h.join().expect("worker thread panicked"))
            .collect()
    });
    for (k, r) in results.into_iter().flatten() {
        slots[k] = Some(r);
    }
    slots.into_iter().map(|r| r.unwrap()).collect()
}
