use std::net::SocketAddr;
use std::time::Instant;

use axum::{
    extract::Query,
    http::StatusCode,
    response::{IntoResponse, Json},
    routing::{get, post},
    Router,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

#[derive(Deserialize)]
struct WorkParams {
    #[serde(default = "default_iterations")]
    iterations: u64,
}

fn default_iterations() -> u64 {
    10_000
}

#[derive(Serialize)]
struct WorkResponse {
    iterations: u64,
    elapsed_ms: f64,
}

#[derive(Serialize)]
struct HealthResponse {
    status: &'static str,
}

async fn health() -> Json<HealthResponse> {
    Json(HealthResponse { status: "ok" })
}

async fn work(Query(params): Query<WorkParams>) -> impl IntoResponse {
    if params.iterations < 1 || params.iterations > 10_000_000 {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({
                "error": "iterations must be between 1 and 10_000_000"
            })),
        )
            .into_response();
    }

    let start = Instant::now();
    let mut data: Vec<u8> = b"seed".to_vec();
    for _ in 0..params.iterations {
        let mut hasher = Sha256::new();
        hasher.update(&data);
        data = hasher.finalize().to_vec();
    }
    let elapsed_ms = start.elapsed().as_secs_f64() * 1000.0;

    Json(WorkResponse {
        iterations: params.iterations,
        elapsed_ms: (elapsed_ms * 100.0).round() / 100.0,
    })
    .into_response()
}

#[tokio::main(flavor = "multi_thread")]
async fn main() {
    let app = Router::new()
        .route("/health", get(health))
        .route("/work", post(work));

    let addr = SocketAddr::from(([0, 0, 0, 0], 8000));
    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
    println!("rust-api listening on {}", addr);
    axum::serve(listener, app).await.unwrap();
}
