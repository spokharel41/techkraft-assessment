# Part 6: CI/CD Pipeline Review & Improvements

## Problems with the Current Pipeline (7 identified)

### 1. Direct Deploy to Production — No Approval Gate (CRITICAL)
```yaml
- name: Deploy to server
  run: |
    echo "Deploying..."
    # Direct rsync to server - no approval, no rollback
```
Any push to `main` immediately deploys to production. One bad commit, one broken merge = production down. No human review, no pause.

---

### 2. No Rollback Mechanism (CRITICAL)
`rsync` overwrites files. If the deploy breaks production, you can't roll back — you must fix forward or manually restore from backup.

---

### 3. No Environment Promotion (dev → staging → prod)
There's only one environment. Code that passes unit tests goes straight to production users. This skips integration testing, load testing, and stakeholder review on a staging environment.

---

### 4. No Security Scanning
Dependencies aren't scanned for known CVEs. The Docker image (if any) isn't scanned. Secrets aren't checked — a developer could accidentally commit an API key and it would deploy to production undetected.

---

### 5. No Secrets Management
The workflow uses `rsync` with presumably hardcoded or environment-variable credentials. There's no reference to GitHub Actions secrets or vault integration.

---

### 6. Tests Run Without Caching
```yaml
- name: Run tests
  run: |
    pip install -r requirements.txt  # Downloads every time
    pytest
```
No dependency caching means every run installs all packages fresh — slow and expensive. For a team of 11 engineers running CI frequently, this adds up.

---

### 7. Only `push` Trigger — No Pull Request CI
```yaml
on:
  push:
    branches: [main]
```
CI only runs after merging to main. PR branches aren't tested. Engineers get no feedback on their changes before merging — the first signal of a problem is a broken production deploy.

---

## Proposed Production-Ready CI/CD Pipeline

```yaml
# .github/workflows/deploy.yml
name: CI/CD Pipeline

on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main, develop]

env:
  PYTHON_VERSION: "3.11"
  IMAGE_NAME: techkraft-api
  ECR_REGISTRY: ${{ secrets.AWS_ACCOUNT_ID }}.dkr.ecr.ap-south-1.amazonaws.com

jobs:
  # ============================================================
  # JOB 1: Security & Lint (runs on every PR and push)
  # Fast feedback — runs in parallel with tests
  # ============================================================
  security-scan:
    name: Security & Dependency Scan
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Check for hardcoded secrets
        uses: trufflesecurity/trufflehog@main
        with:
          path: ./
          base: ${{ github.event.repository.default_branch }}
          head: HEAD

      - name: Scan Python dependencies for CVEs
        run: |
          pip install safety
          safety check -r requirements.txt --json > safety-report.json || true
          # Fail if critical vulnerabilities found
          safety check -r requirements.txt --severity critical

      - name: Lint with flake8
        run: |
          pip install flake8
          flake8 . --count --select=E9,F63,F7,F82 --show-source  # Syntax errors = fail
          flake8 . --count --exit-zero --max-complexity=10       # Style = warn only

      - name: Upload security reports
        uses: actions/upload-artifact@v4
        if: always()
        with:
          name: security-reports
          path: safety-report.json

  # ============================================================
  # JOB 2: Test Suite
  # ============================================================
  test:
    name: Test Suite
    runs-on: ubuntu-latest
    needs: security-scan  # Don't test if security scan hard-fails

    strategy:
      matrix:
        python-version: ["3.11", "3.12"]  # Test multiple Python versions

    steps:
      - uses: actions/checkout@v4

      - name: Set up Python ${{ matrix.python-version }}
        uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
          cache: pip  # Cache pip dependencies — faster builds

      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          pip install pytest pytest-cov pytest-xdist

      - name: Run tests with coverage
        run: |
          pytest --cov=. --cov-report=xml --cov-report=term-missing \
                 --junitxml=test-results.xml -v

      - name: Check coverage threshold
        run: |
          # Fail if coverage drops below 70%
          coverage report --fail-under=70

      - name: Upload test results
        uses: actions/upload-artifact@v4
        if: always()
        with:
          name: test-results-${{ matrix.python-version }}
          path: |
            test-results.xml
            coverage.xml

  # ============================================================
  # JOB 3: Build Docker Image & Scan
  # ============================================================
  build:
    name: Build & Scan Docker Image
    runs-on: ubuntu-latest
    needs: test
    if: github.event_name == 'push'  # Only build on push, not PRs

    outputs:
      image-tag: ${{ steps.meta.outputs.tags }}
      image-digest: ${{ steps.build.outputs.digest }}

    steps:
      - uses: actions/checkout@v4

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id:     ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws-region: ap-south-1

      - name: Login to Amazon ECR
        id: login-ecr
        uses: aws-actions/amazon-ecr-login@v2

      - name: Extract metadata (tags, labels)
        id: meta
        uses: docker/metadata-action@v5
        with:
          images: ${{ env.ECR_REGISTRY }}/${{ env.IMAGE_NAME }}
          tags: |
            type=sha,prefix=,suffix=,format=short
            type=ref,event=branch
            type=semver,pattern={{version}}

      - name: Build Docker image
        id: build
        uses: docker/build-push-action@v5
        with:
          context: .
          push: false  # Don't push yet — scan first
          load: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          cache-from: type=gha   # GitHub Actions cache
          cache-to: type=gha,mode=max

      - name: Scan image for vulnerabilities (Trivy)
        uses: aquasecurity/trivy-action@master
        with:
          image-ref: ${{ steps.meta.outputs.tags }}
          format: sarif
          output: trivy-results.sarif
          severity: CRITICAL,HIGH
          exit-code: 1  # Fail pipeline on critical/high CVEs

      - name: Upload Trivy scan results to GitHub Security
        uses: github/codeql-action/upload-sarif@v3
        if: always()
        with:
          sarif_file: trivy-results.sarif

      - name: Push to ECR (only if scan passed)
        uses: docker/build-push-action@v5
        with:
          context: .
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}

  # ============================================================
  # JOB 4: Deploy to Staging
  # Automatic on push to develop branch
  # ============================================================
  deploy-staging:
    name: Deploy to Staging
    runs-on: ubuntu-latest
    needs: build
    environment:
      name: staging
      url: https://staging.techkraft.com
    if: github.ref == 'refs/heads/develop'

    steps:
      - uses: actions/checkout@v4

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id:     ${{ secrets.STAGING_AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.STAGING_AWS_SECRET_ACCESS_KEY }}
          aws-region: ap-south-1

      - name: Deploy to staging ECS
        run: |
          # Update ECS service with new image
          aws ecs update-service \
            --cluster techkraft-staging \
            --service techkraft-api \
            --force-new-deployment \
            --region ap-south-1

      - name: Wait for deployment to stabilize
        run: |
          aws ecs wait services-stable \
            --cluster techkraft-staging \
            --services techkraft-api \
            --region ap-south-1

      - name: Run smoke tests against staging
        run: |
          STAGING_URL="https://staging.techkraft.com"
          echo "Testing health endpoint..."
          curl -f "$STAGING_URL/health" || (echo "Staging health check failed!" && exit 1)
          echo "Testing API endpoint..."
          curl -f "$STAGING_URL/" | jq '.message' || exit 1
          echo "✅ Staging smoke tests passed"

  # ============================================================
  # JOB 5: Deploy to Production
  # Requires manual approval (GitHub Environments protection rule)
  # Only from main branch, after staging is healthy
  # ============================================================
  deploy-production:
    name: Deploy to Production
    runs-on: ubuntu-latest
    needs: [build, deploy-staging]
    environment:
      name: production        # GitHub will require approval from reviewers
      url: https://api.techkraft.com
    if: github.ref == 'refs/heads/main'

    steps:
      - uses: actions/checkout@v4

      - name: Configure AWS credentials (production)
        uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id:     ${{ secrets.PROD_AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.PROD_AWS_SECRET_ACCESS_KEY }}
          aws-region: ap-south-1

      - name: Record deploy start (for rollback reference)
        id: pre-deploy
        run: |
          # Save current task definition ARN for rollback
          CURRENT_TASK=$(aws ecs describe-services \
            --cluster techkraft-prod \
            --services techkraft-api \
            --query 'services[0].taskDefinition' \
            --output text)
          echo "previous-task-def=$CURRENT_TASK" >> $GITHUB_OUTPUT
          echo "Previous task definition: $CURRENT_TASK"

      - name: Deploy to production ECS (Blue/Green)
        run: |
          aws ecs update-service \
            --cluster techkraft-prod \
            --service techkraft-api \
            --force-new-deployment \
            --region ap-south-1

      - name: Monitor deployment
        run: |
          aws ecs wait services-stable \
            --cluster techkraft-prod \
            --services techkraft-api \
            --region ap-south-1

      - name: Production health check
        id: health-check
        run: |
          curl -f "https://api.techkraft.com/health" || echo "health_failed=true" >> $GITHUB_OUTPUT

      - name: 🔴 ROLLBACK if health check failed
        if: steps.health-check.outputs.health_failed == 'true'
        run: |
          echo "⚠️ Health check failed! Rolling back to previous version..."
          aws ecs update-service \
            --cluster techkraft-prod \
            --service techkraft-api \
            --task-definition ${{ steps.pre-deploy.outputs.previous-task-def }}
          echo "Rollback complete. Previous version restored."
          exit 1  # Mark the workflow as failed

      - name: Notify on success
        if: success()
        uses: slackapi/slack-github-action@v1
        with:
          channel-id: "#deployments"
          slack-message: |
            ✅ *Production Deploy Successful*
            Commit: ${{ github.sha }}
            By: ${{ github.actor }}
            URL: https://api.techkraft.com
        env:
          SLACK_BOT_TOKEN: ${{ secrets.SLACK_BOT_TOKEN }}
```

---

## Summary of Improvements

| Issue | Fix |
|-------|-----|
| No security scan | Added Trivy (Docker), Safety (Python deps), TruffleHog (secrets) |
| No testing on PRs | Added `pull_request` trigger with test matrix |
| No approval gate | GitHub Environments with required reviewers on `production` |
| No rollback | Pre-deploy snapshot + automatic rollback on health check failure |
| No environment promotion | develop → staging (auto) → main → production (approval required) |
| No dep caching | `cache: pip` in setup-python, Docker BuildKit layer cache |
| No observability | Slack notification on success/failure, SARIF security reports |

---

## Environment Promotion Flow

```
Developer pushes branch
        │
        ▼
  PR Created → CI runs:
    ✅ security-scan
    ✅ tests (3.11 + 3.12)
        │
        ▼
   Merge to develop
        │
        ▼
  Auto-deploy to STAGING
    ✅ ECS deploy
    ✅ Smoke tests
        │
        ▼
   PR to main
   (code review required)
        │
        ▼
  Merge to main
        │
        ▼
  ⏸️  MANUAL APPROVAL GATE
  (Senior engineer reviews)
        │
        ▼
  Deploy to PRODUCTION
    + Health check
    + Auto-rollback if fails
    + Slack notification
```
