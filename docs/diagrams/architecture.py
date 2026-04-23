"""
Container-level architecture diagram (C4 Level 2) for the Rust + AWS SaaS.

Generates two PNGs in this directory:
  - architecture_main.png       (request path + data plane — "hero" diagram)
  - architecture_observability.png (telemetry topology)

Dependencies:
  pip install diagrams
  brew install graphviz            # or: apt-get install graphviz

Run:
  python docs/diagrams/architecture.py
"""

from diagrams import Diagram, Cluster, Edge
from diagrams.aws.compute import Fargate, ECR
from diagrams.aws.database import RDS, ElastiCache
from diagrams.aws.network import CloudFront, ELB, NATGateway, Endpoint
from diagrams.aws.security import WAF, Shield, SecretsManager, KMS, Guardduty
from diagrams.aws.storage import S3
from diagrams.aws.management import CloudwatchLogs, SystemsManagerAppConfig
from diagrams.aws.devtools import Codedeploy, XRay
from diagrams.aws.analytics import KinesisDataFirehose
from diagrams.onprem.client import Users
from diagrams.onprem.ci import GithubActions
from diagrams.onprem.monitoring import Grafana, Prometheus
from diagrams.onprem.compute import Server
from diagrams.saas.alerting import Pagerduty


SHARED_GRAPH_ATTR = {
    "fontsize": "16",
    "bgcolor": "white",
    "pad": "0.4",
    "splines": "spline",
    "nodesep": "0.6",
    "ranksep": "0.8",
}


def render_main_diagram() -> None:
    """Request path + data plane — the hero diagram."""
    with Diagram(
        "Rust + AWS SaaS — Request path & data plane",
        filename="architecture_main",
        outformat="png",
        show=False,
        direction="TB",
        graph_attr=SHARED_GRAPH_ATTR,
    ):
        clients = Users("API clients\n(devs/backends)")

        with Cluster("Edge (global)"):
            shield = Shield("Shield Std")
            waf = WAF("WAF\nCRS + rate-based")
            cf = CloudFront("CloudFront\nTLS 1.3 · HSTS")

        with Cluster("VPC us-east-1  —  3 AZs"):
            with Cluster("Public subnets"):
                alb = ELB("ALB\nSG: CF-only\n+ X-Origin-Secret")
                nat = NATGateway("NAT GW × 3\n(AZ-local)")

            with Cluster("Private-app subnets"):
                fargate_light = Fargate(
                    "api-lightweight\n(Rust, Graviton)\nlookups + validators"
                )
                fargate_tr = Fargate(
                    "api-transforms\n(Rust + Chromium)\nscreenshot · scrape"
                )

            with Cluster("Private-data subnets (no egress)"):
                rds = RDS("Postgres\nMulti-AZ\ndb.m7g.large")
                valkey = ElastiCache("Valkey\nMulti-AZ\ncache.t4g.small")

            vpce = Endpoint("VPC endpoints\nS3 · ECR · Logs · Secrets")

        with Cluster("Object storage"):
            s3_assets = S3("Reference datasets\n(MaxMind, etc.)")
            s3_outputs = S3("Transform outputs\n(content-addressed)")

        with Cluster("Third parties"):
            vendors = Server("Data vendors")
            validators_ext = Server("VIES · MX · SMTP")
            proxies = Server("Scraping proxies")

        with Cluster("Secrets & KMS"):
            sm = SecretsManager("Secrets Manager\n(rotated)")
            kms = KMS("KMS CMKs\n(multi-region)")
            gd = Guardduty("GuardDuty")

        kfh = KinesisDataFirehose("CF real-time logs\n→ Firehose\n(billing counter)")

        # Main request path
        clients >> Edge(label="HTTPS") >> cf
        cf >> Edge(style="dashed", color="firebrick") >> waf
        cf >> Edge(style="dashed", color="firebrick") >> shield
        cf >> Edge(label="via prefix-list\n+ header secret") >> alb
        alb >> [fargate_light, fargate_tr]

        # Data plane
        [fargate_light, fargate_tr] >> valkey
        [fargate_light, fargate_tr] >> rds
        [fargate_light, fargate_tr] >> Edge(style="dotted") >> vpce
        vpce >> Edge(style="dotted") >> [s3_assets, sm]

        # Egress
        [fargate_light, fargate_tr] >> Edge(color="gray", style="dotted") >> nat
        nat >> Edge(color="gray") >> [vendors, validators_ext, proxies]

        # Transforms output
        fargate_tr >> Edge(label="put") >> s3_outputs
        cf >> Edge(style="dashed", label="serve cached\noutputs") >> s3_outputs

        # Billing
        cf >> Edge(style="dashed", color="darkgreen") >> kfh >> valkey

        # Encryption dependency
        kms >> Edge(style="dotted", color="gray50") >> [rds, sm, s3_assets, s3_outputs]


def render_observability_diagram() -> None:
    """Telemetry topology: OTel → AMP/X-Ray/CW → Grafana → PagerDuty."""
    with Diagram(
        "Rust + AWS SaaS — Observability topology",
        filename="architecture_observability",
        outformat="png",
        show=False,
        direction="LR",
        graph_attr=SHARED_GRAPH_ATTR,
    ):
        with Cluster("Fargate task"):
            app = Fargate("Rust app\n(tracing crate)")
            otel = Server("OTel Collector\n(sidecar)")
            app >> Edge(label="OTLP/gRPC\nlocalhost") >> otel

        with Cluster("Backends"):
            amp = Prometheus("Amazon Managed\nPrometheus")
            xray = XRay("X-Ray\n10% + 100% errors")
            cwl = CloudwatchLogs("CloudWatch Logs\n14d retention")
            s3_archive = S3("S3 archive\n(Glacier)")

        with Cluster("Presentation & alerting"):
            amg = Grafana("Managed Grafana\n(SSO)")
            pd = Pagerduty("PagerDuty\n(burn rate alerts)")

        with Cluster("CI/CD (context)"):
            gha = GithubActions("GitHub Actions\nOIDC")
            ecr = ECR("ECR\n(signed images)")
            cd = Codedeploy("CodeDeploy\nBlue/Green\n+ auto-rollback")
            appcfg = SystemsManagerAppConfig("AppConfig\nfeature flags")

        otel >> Edge(label="metrics") >> amp
        otel >> Edge(label="traces") >> xray
        otel >> Edge(label="logs") >> cwl
        cwl >> Edge(style="dashed", label="subscription") >> s3_archive

        [amp, xray, cwl] >> amg >> pd
        cd >> Edge(style="dashed", label="CW alarms\nfeedback loop") >> amg

        gha >> ecr >> cd >> app
        appcfg >> Edge(style="dotted") >> app


if __name__ == "__main__":
    render_main_diagram()
    render_observability_diagram()
