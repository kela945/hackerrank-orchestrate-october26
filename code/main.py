#!/usr/bin/env python3
"""
Ghost Data Still Exists - Deletion Audit Agent
Determines whether user personal data deletion was actually completed.
"""

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import sqlite3

REPO_ROOT = Path(__file__).parent.parent
DATASET_DIR = REPO_ROOT / "dataset"


def load_csv(filename: str) -> List[Dict[str, str]]:
    """Load CSV file from dataset directory."""
    filepath = DATASET_DIR / filename
    data = []
    with open(filepath, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            data.append(row)
    return data


class DeletionAudit:
    """Orchestrates deletion audit logic."""

    def __init__(self):
        """Initialize with all dataset files."""
        print("Loading datasets...")
        self.deletion_requests = load_csv('deletion_requests.csv')
        self.user_identities = load_csv('user_identities.csv')
        self.user_audit_log = load_csv('user_audit_log.csv')
        self.system_activity = load_csv('system_activity.csv')
        self.deletion_rules = load_csv('deletion_rules.csv')
        self.evidence_files = load_csv('evidence_files.csv')
        print(f"Loaded {len(self.deletion_requests)} requests")

        # Build indices for fast lookup
        self._build_indices()

    def _build_indices(self):
        """Create lookup indices for efficient data access."""
        self.user_by_email = {}
        self.user_by_id = {}

        for user in self.user_identities:
            user_id = user['user_id']
            email = user['email'].lower()
            self.user_by_id[user_id] = user
            self.user_by_email[email] = user

        self.audit_log_by_user = {}
        for log in self.user_audit_log:
            user_id = log['user_id']
            if user_id not in self.audit_log_by_user:
                self.audit_log_by_user[user_id] = []
            self.audit_log_by_user[user_id].append(log)

        self.rules_by_category = {}
        for rule in self.deletion_rules:
            category = rule['data_category']
            if category not in self.rules_by_category:
                self.rules_by_category[category] = []
            self.rules_by_category[category].append(rule)

        self.evidence_by_id = {}
        for evidence in self.evidence_files:
            self.evidence_by_id[evidence['file_id']] = evidence

        # Index activity by user and time
        self.activity_by_resource = {}
        for activity in self.system_activity:
            resource_id = activity.get('resource_id', '')
            if resource_id not in self.activity_by_resource:
                self.activity_by_resource[resource_id] = []
            self.activity_by_resource[resource_id].append(activity)

    def resolve_user(self, email: str) -> Optional[Dict[str, str]]:
        """
        Resolve user from email (current or historical).
        Returns user identity or None.
        """
        email_lower = email.lower()

        # Check current identities
        if email_lower in self.user_by_email:
            return self.user_by_email[email_lower]

        # Check historical audit log for this email
        for log in self.user_audit_log:
            old_email = log.get('old_email', '').lower()
            new_email = log.get('new_email', '').lower()
            if old_email == email_lower or new_email == email_lower:
                user_id = log['user_id']
                if user_id in self.user_by_id:
                    return self.user_by_id[user_id]

        return None

    def get_all_identifiers(self, user_id: str) -> List[str]:
        """Get all current and historical identifiers for a user."""
        identifiers = []

        if user_id in self.user_by_id:
            user = self.user_by_id[user_id]
            email = user['email']
            phone = user['phone']
            email_hash = user['email_hash']

            if email:
                identifiers.append(email.lower())
                identifiers.append(email)
            if phone:
                identifiers.append(phone)
            if email_hash:
                identifiers.append(email_hash)

        # Add historical identifiers from audit log
        if user_id in self.audit_log_by_user:
            for log in self.audit_log_by_user[user_id]:
                old_val = log.get('old_value', '').strip()
                new_val = log.get('new_value', '').strip()
                if old_val:
                    identifiers.append(old_val)
                    if log.get('field_name') == 'email':
                        identifiers.append(old_val.lower())
                if new_val:
                    identifiers.append(new_val)
                    if log.get('field_name') == 'email':
                        identifiers.append(new_val.lower())

        return list(set(i for i in identifiers if i and len(i) > 1))

    def is_deletion_requested(self, request_text: str) -> bool:
        """
        Determine if request_text authorizes account deletion.
        Returns True if deletion is requested, False otherwise.
        """
        text_lower = request_text.lower()

        # Deletion keywords (multiple languages)
        deletion_keywords = [
            'delete', 'remove', 'erase', 'permanently',
            'close account', 'account closure', 'account deleted',
            'suppress', 'suppress my', 'suppress data',
            'request to delete', 'please delete',
            'elimination', 'eliminate',
            'deletion', 'deleting', 'closed',
            'remove my', 'remove the account', 'delete my account',
            'delete the account', 'erase my', 'remove records',
            'deletion request', 'treat this message as',
            'permanent account deletion', 'permanent deletion',
            'eliminar mi', 'eliminar', 'borrar',
            'suppression', 'supprimer',
            'loschen', 'entfernen'
        ]

        # Check for deletion keywords
        has_deletion = any(keyword in text_lower for keyword in deletion_keywords)

        if not has_deletion:
            return False

        # Strong non-deletion indicators
        strong_exclusions = [
            'unsubscribe from marketing',
            'opt-out from marketing',
            'export my data',
            'download my data',
            'correct my information',
            'rectify my data',
            'update my information',
            'change my email',
            'request for access',
            'data access request'
        ]

        has_strong_exclusion = any(excl in text_lower for excl in strong_exclusions)

        return not has_strong_exclusion

    def find_data_in_evidence(self, identifiers: List[str]) -> Dict[str, List[str]]:
        """Find what data is present in evidence files for given identifiers."""
        found_records = {}

        for file_id, evidence in self.evidence_by_id.items():
            filepath = DATASET_DIR / evidence['file_path']

            if not filepath.exists():
                continue

            found_in_file = []

            try:
                if filepath.suffix == '.json':
                    with open(filepath, 'r', encoding='utf-8') as f:
                        content = f.read()
                        for identifier in identifiers:
                            if identifier and len(identifier) > 2 and identifier in content:
                                found_in_file.append('account_service')
                                break

                elif filepath.suffix == '.log' or filepath.suffix == '.txt':
                    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                        for identifier in identifiers:
                            if identifier and len(identifier) > 2 and identifier in content:
                                if 'search' in filepath.name.lower():
                                    found_in_file.append('search_service')
                                elif 'analytics' in filepath.name.lower():
                                    found_in_file.append('analytics_platform')
                                elif 'support' in filepath.name.lower():
                                    found_in_file.append('support_platform')
                                elif 'backup' in filepath.name.lower():
                                    found_in_file.append('backup_service')
                                else:
                                    found_in_file.append('account_service')
                                break

                elif filepath.suffix == '.sql':
                    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                        for identifier in identifiers:
                            if identifier and len(identifier) > 2 and identifier in content:
                                found_in_file.append('account_service')
                                break

            except Exception:
                pass

            if found_in_file:
                found_records[file_id] = found_in_file

        return found_records

    def generate_steps(self, user_id: str, evidence_found: Dict[str, List[str]]) -> Tuple[str, List[Dict[str, Any]]]:
        """Generate remediation and verification steps."""

        if not evidence_found:
            # No evidence found - deletion complete
            account_id = f"account_{user_id.split('_')[1]}" if '_' in user_id else f"account_{user_id}"
            steps = [{
                'step_no': 1,
                'phase': 'VERIFICATION',
                'system_id': 'account_service',
                'resource_id': account_id,
                'operation': 'VERIFY_RECORD_ABSENT',
                'state': 'ABSENT',
                'evidence_id': 'evidence_01'
            }]
            return 'COMPLETE', steps

        # Data found - deletion incomplete
        steps = []
        step_no = 1

        # Findings phase
        account_id = f"account_{user_id.split('_')[1]}" if '_' in user_id else f"account_{user_id}"
        first_evidence = list(evidence_found.keys())[0]

        # Report remaining records
        systems_with_data = set()
        for file_id, systems in evidence_found.items():
            for system in systems:
                systems_with_data.add(system)

        # Add finding for account
        steps.append({
            'step_no': step_no,
            'phase': 'FINDING',
            'system_id': 'account_service',
            'resource_id': account_id,
            'operation': 'REPORT_REMAINING_RECORD',
            'state': 'PRESENT',
            'evidence_id': first_evidence
        })
        step_no += 1

        # Add findings for other systems
        for system in sorted(systems_with_data):
            if system != 'account_service':
                resource_map = {
                    'search_service': 'search_001',
                    'analytics_platform': 'analytics_001',
                    'support_platform': 'support_001',
                    'backup_service': 'backup_001',
                    'integration_service': 'data_flow_001'
                }
                resource_id = resource_map.get(system, f'{system}_001')
                steps.append({
                    'step_no': step_no,
                    'phase': 'FINDING',
                    'system_id': system,
                    'resource_id': resource_id,
                    'operation': 'REPORT_REMAINING_RECORD',
                    'state': 'PRESENT',
                    'evidence_id': first_evidence
                })
                step_no += 1

        # Remediation phase
        steps.append({
            'step_no': step_no,
            'phase': 'REMEDIATION',
            'system_id': 'account_service',
            'resource_id': account_id,
            'operation': 'DELETE_RECORD',
            'state': 'ABSENT',
            'evidence_id': ''
        })
        step_no += 1

        for system in sorted(systems_with_data):
            if system != 'account_service':
                resource_map = {
                    'search_service': 'search_001',
                    'analytics_platform': 'analytics_001',
                    'support_platform': 'support_001',
                    'backup_service': 'backup_001',
                    'integration_service': 'data_flow_001'
                }
                resource_id = resource_map.get(system, f'{system}_001')
                steps.append({
                    'step_no': step_no,
                    'phase': 'REMEDIATION',
                    'system_id': system,
                    'resource_id': resource_id,
                    'operation': 'DELETE_RECORD',
                    'state': 'ABSENT',
                    'evidence_id': ''
                })
                step_no += 1

        # Verification phase
        steps.append({
            'step_no': step_no,
            'phase': 'VERIFICATION',
            'system_id': 'account_service',
            'resource_id': account_id,
            'operation': 'VERIFY_RECORD_ABSENT',
            'state': 'ABSENT',
            'evidence_id': ''
        })
        step_no += 1

        for system in sorted(systems_with_data):
            if system != 'account_service':
                resource_map = {
                    'search_service': 'search_001',
                    'analytics_platform': 'analytics_001',
                    'support_platform': 'support_001',
                    'backup_service': 'backup_001',
                    'integration_service': 'data_flow_001'
                }
                resource_id = resource_map.get(system, f'{system}_001')
                steps.append({
                    'step_no': step_no,
                    'phase': 'VERIFICATION',
                    'system_id': system,
                    'resource_id': resource_id,
                    'operation': 'VERIFY_RECORD_ABSENT',
                    'state': 'ABSENT',
                    'evidence_id': ''
                })
                step_no += 1

        # Security audit verification (always required)
        audit_id = f"audit_record_{hash(user_id) % 10000}"
        steps.append({
            'step_no': step_no,
            'phase': 'VERIFICATION',
            'system_id': 'security_service',
            'resource_id': audit_id,
            'operation': 'VERIFY_RECORD_PRESENT',
            'state': 'PRESENT',
            'evidence_id': ''
        })

        return 'INCOMPLETE', steps

    def process_request(self, request: Dict[str, str]) -> Dict[str, Any]:
        """Process a single deletion request and return audit result."""
        case_id = request['case_id']
        email = request['email']
        request_text = request['request_text']

        # Step 1: Check if deletion was actually requested
        if not self.is_deletion_requested(request_text):
            return {
                'case_id': case_id,
                'deletion_status': 'NOT_APPLICABLE',
                'steps_json': '[]'
            }

        # Step 2: Resolve user identity
        user = self.resolve_user(email)
        if not user:
            return {
                'case_id': case_id,
                'deletion_status': 'NOT_APPLICABLE',
                'steps_json': '[]'
            }

        user_id = user['user_id']
        identifiers = self.get_all_identifiers(user_id)

        # Step 3: Find data in evidence files
        evidence_found = self.find_data_in_evidence(identifiers)

        # Step 4: Generate remediation steps
        status, steps = self.generate_steps(user_id, evidence_found)

        return {
            'case_id': case_id,
            'deletion_status': status,
            'steps_json': json.dumps(steps)
        }

    def run(self):
        """Process all deletion requests and write output."""
        output_data = []

        for i, request in enumerate(self.deletion_requests, 1):
            result = self.process_request(request)
            output_data.append(result)

            if i % 100 == 0:
                print(f"Processed {i}/{len(self.deletion_requests)} requests")

        # Sort by case_id properly (numeric sort)
        output_data.sort(key=lambda x: int(x['case_id'].split('_')[1]))

        # Write output.csv
        output_file = REPO_ROOT / 'output.csv'
        with open(output_file, 'w', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['case_id', 'deletion_status', 'steps_json'])
            writer.writeheader()
            writer.writerows(output_data)

        print(f"\nWrote {len(output_data)} results to {output_file}")

        # Print statistics
        statuses = {}
        for row in output_data:
            status = row['deletion_status']
            statuses[status] = statuses.get(status, 0) + 1

        print("\nResults Summary:")
        for status in ['NOT_APPLICABLE', 'COMPLETE', 'INCOMPLETE']:
            if status in statuses:
                print(f"  {status}: {statuses[status]}")

        return output_data


if __name__ == '__main__':
    audit = DeletionAudit()
    audit.run()
