# `/work`: mismo algoritmo, distinto rendimiento

Este documento explica por qué el endpoint `POST /work` responde más rápido en la API de Rust que en la de Python, **a pesar de ejecutar el mismo algoritmo**. El foco no está en "cuál gana" sino en **de dónde sale la diferencia**.

---

## El algoritmo

Ambas implementaciones hacen exactamente lo mismo: SHA-256 iterativo, encadenando la salida como entrada de la siguiente iteración, partiendo de `b"seed"`.

**Python** ([api/main.py](../api/main.py)):
```python
data = b"seed"
for _ in range(iterations):
    data = hashlib.sha256(data).digest()
```

**Rust** ([api-rust/src/main.rs](../api-rust/src/main.rs)):
```rust
let mut data: Vec<u8> = b"seed".to_vec();
for _ in 0..params.iterations {
    let mut hasher = Sha256::new();
    hasher.update(&data);
    data = hasher.finalize().to_vec();
}
```

Mismo patrón, mismo costo teórico: `N` llamadas a SHA-256 sobre 32 bytes (la primera sobre 4) + una allocation chica por iteración en ambos casos (`bytes` en Python, `Vec<u8>` en Rust). El algoritmo es **justo**: no hay ventaja estructural para ninguno.

---

## Por qué entonces el tiempo difiere

La diferencia no está en "qué se computa" sino en **cuánto cuesta ejecutar cada iteración del loop y atender cada request**. Son varias capas sumándose:

### 1. Overhead del loop: intérprete vs código nativo

- **Python**: cada vuelta del `for` es bytecode interpretado. Por iteración se paga: dispatch de opcodes, resolución del atributo `hashlib.sha256`, construcción del objeto hasher, llamada a método, boxing del resultado en un `bytes`. El trabajo "útil" (hashear 32 bytes) dura nanosegundos; el overhead del intérprete puede ser del mismo orden o mayor.
- **Rust**: el loop se compila a un puñado de instrucciones máquina. LLVM inlinea, elimina bounds checks donde puede y mantiene los datos en registros. El overhead por iteración tiende a cero — solo queda el costo real del hash.

**Efecto**: con `iterations` bajo (ej. 1.000–10.000), el loop de Python gasta más tiempo **interpretando** que **hasheando**.

### 2. Frontera FFI Python↔C

`hashlib` en Python es un wrapper sobre OpenSSL (código C). Cada llamada a `hashlib.sha256(data).digest()` cruza la frontera Python→C→Python: convertir argumentos, liberar el GIL, ejecutar, re-adquirir GIL, crear `PyObject` con el resultado. Con 10.000 iteraciones son 10.000 cruces de frontera.

Rust no paga esto: el código de `sha2` está linkeado directamente y se inlinea en el mismo binario.

### 3. Implementación de SHA-256

- **Python/hashlib → OpenSSL**: en CPUs modernas x86_64 y ARM64, OpenSSL usa las instrucciones hardware **SHA-NI** (Intel/AMD) o el acelerador criptográfico de ARMv8. El costo *por hash* es muy bajo.
- **Rust/sha2 crate**: tal como está en `Cargo.toml`, usa la implementación portable en Rust puro. **No tiene activado el feature `asm`**, así que no aprovecha SHA-NI.

Esto es contraintuitivo: **por iteración aislada, Python (con OpenSSL+SHA-NI) puede ser competitivo o incluso más rápido que Rust en SHA-256**. La ventaja de Rust no viene del hash; viene de todo lo que lo rodea.

> Si se activa `sha2 = { version = "0.10", features = ["asm"] }` en Cargo.toml, Rust también usa SHA-NI y la brecha por-hash se cierra. Se deja sin activar a propósito para que el benchmark refleje el default del ecosistema.

### 4. Modelo de concurrencia bajo carga

- **Python (FastAPI + uvicorn)**: `def work(...)` es un handler **sync**. Starlette lo ejecuta en un threadpool (anyio). El SHA-256 en OpenSSL libera el GIL, pero el `for` y la aritmética de Python **no**. Bajo carga concurrente, los loops compiten por el GIL y la latencia p99 explota. El workaround clásico es correr múltiples workers de uvicorn (procesos), lo que multiplica memoria.
- **Rust (axum + tokio multi-thread)**: el handler es `async` pero el loop es CPU-bound síncrono. Corre sobre los workers de tokio, que están bindeados a cores. No hay GIL, así que N cores procesan N requests en paralelo real.

**Efecto**: a 1 request/s la diferencia es moderada; a 100 RPS concurrentes, Python se serializa mientras Rust escala casi lineal hasta saturar cores.

### 5. Overhead por request (fuera del loop)

Cada request `POST /work` paga costos ajenos al hashing:

| Capa | Python (uvicorn + FastAPI) | Rust (axum + tokio) |
|---|---|---|
| Parser HTTP | httptools/h11 en Python + C | `hyper` en Rust, zero-copy |
| Routing | Starlette, introspección runtime | axum, resuelto en compile-time |
| Validación de query | Pydantic (construye modelo) | serde (deserialización monomórfica) |
| Serialización respuesta | `json.dumps` + encoding | `serde_json` con buffers reusables |
| Scheduling | threadpool + GIL | tarea tokio en worker pool |

En `iterations=100` este overhead **domina** el tiempo total: el loop hashea en <1 ms, pero atender el request cuesta varios ms en Python y sub-ms en Rust.

### 6. Cold start y footprint (relevante para HPA)

No afecta latencia steady-state pero sí el comportamiento bajo autoscaling:

- Imagen Python ~150 MB, arranca uvicorn + importa FastAPI/Pydantic: ~1–2 s hasta `/health` OK.
- Binario Rust estático ~10–20 MB, arranca en <100 ms.

Con HPA agresivo, Rust agrega capacidad mucho antes ante un pico.

---

## Dónde pesa cada factor según `iterations`

| `iterations` | Factor que domina | Gap Rust/Python esperado |
|---|---|---|
| 100 | Overhead de request (HTTP + framework) | Grande (5–10×) |
| 10.000 | Overhead del loop + FFI en Python | Grande (3–5×) |
| 1.000.000 | Costo real del hash | Chico (Python con SHA-NI puede estar cerca si Rust no usa `asm`) |
| Bajo concurrencia alta | Modelo de concurrencia (GIL vs multi-thread) | Muy grande |

---

## Resumen

El algoritmo es idéntico y justo. La diferencia de rendimiento se explica por capas apiladas, **en orden de impacto típico**:

1. **Overhead del request** (framework + HTTP stack) — domina en cargas cortas.
2. **Overhead del loop interpretado** + FFI por iteración — domina en cargas medias.
3. **Modelo de concurrencia** (GIL serializa, tokio paraleliza) — domina bajo carga concurrente.
4. **Implementación del hash** — el único factor donde Python puede estar a la par, gracias a OpenSSL+SHA-NI.
5. **Cold start / footprint** — no es latencia de request pero mueve la aguja con HPA.

La lectura útil no es "Rust es más rápido que Python", sino **qué parte del pipeline introduce cada tecnología**: Python paga interpretación y concurrencia; Rust paga complejidad de desarrollo. Para un endpoint CPU-bound, Rust gana; para un endpoint I/O-bound esperando una DB, la diferencia se vuelve marginal.
