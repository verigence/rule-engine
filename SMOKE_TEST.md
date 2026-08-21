# verigence-audit — Sub-Task 10 checklist
#
# Run these locally after cloning to verify the full stack.
#
# Prerequisites:
#   - PostgreSQL running (same instance as verigence-di, or a local copy)
#   - Python 3.12
#   - pip install -e "backend/[dev]"
#
# 1. Copy env file and fill in DB URLs:
#      cp infra/.env.local.example infra/.env.local
#      # Edit AUDIT_DB_URL and AUDIT_DI_DB_URL
#
# 2. Run migrations:
#      cd backend && alembic upgrade head
#      # Expected: audit schema + audit_rules (85 rows) + audit_runs + audit_findings
#
# 3. Verify seed count:
#      psql $AUDIT_DB_URL -c "SELECT COUNT(*) FROM audit.audit_rules;"
#      # Expected: 85
#      psql $AUDIT_DB_URL -c "SELECT COUNT(*) FROM audit.audit_rules WHERE audit_scope='CROSS_CASE';"
#      # Expected: 6
#
# 4. Run tests:
#      cd backend && pytest
#      # Expected: all pass, coverage >= 70%
#
# 5. Start service:
#      uvicorn verigence.audit.main:create_app --factory --reload --app-dir backend/src
#
# 6. Liveness check:
#      curl http://localhost:8000/health/live
#      # Expected: {"status": "ok"}
#
# 7. Webhook smoke test (no auth — server-to-server):
#      curl -s -X POST http://localhost:8000/internal/trigger \
#        -H "X-Webhook-Secret: local-dev-secret" \
#        -H "Content-Type: application/json" \
#        -d '{"tenant_id":"t1","subject_id":"00000000-0000-0000-0000-000000000001","doc_type_key":"booking_docket"}'
#      # Expected: {"accepted":true,"subject_id":"..."}
#
# 8. Full audit (mock JWT):
#      curl -s -X POST \
#        'http://localhost:8000/v1/tenants/t1/subjects/00000000-0000-0000-0000-000000000001/audit' \
#        -H "Authorization: Bearer mock.t1.admin.TENANT_ADMIN"
#      # Expected: {"errorCode":"000", "data":{"verdict":"INSUFFICIENT_DATA",...}}
#
# 9. List rules:
#      curl -s 'http://localhost:8000/v1/tenants/t1/audit/rules' \
#        -H "Authorization: Bearer mock.t1.admin.TENANT_ADMIN" | python3 -m json.tool | head -40
#
# 10. Cross-case scan:
#      curl -s -X POST 'http://localhost:8000/v1/tenants/t1/audit/cross-case-scan' \
#        -H "Authorization: Bearer mock.t1.admin.TENANT_ADMIN"
#
# 11. Phase audit (booking phase):
#      curl -s -X POST \
#        'http://localhost:8000/v1/tenants/t1/subjects/00000000-0000-0000-0000-000000000001/audit/booking' \
#        -H "Authorization: Bearer mock.t1.admin.TENANT_ADMIN" \
#        -H "Content-Type: application/json" \
#        -d '{"includeSkipped":false}'
