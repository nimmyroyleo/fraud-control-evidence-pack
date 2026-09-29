# Architecture Overview

## Purpose

Generate an audit-ready fraud control evidence pack from source datasets.

## Components

### generate_sample_data.py

Creates sample datasets.

### fraud_control_view.py

Processes datasets and generates HTML evidence packs.

### data/

Stores source CSV files.

### out/

Stores generated HTML reports.

### test_fraud_control_view.py

Validates report generation and application behavior.

## Inputs

- Alert datasets
- Rule definitions
- Rule mappings
- Loan datasets
- Fraud labels
- Review outcomes

## Outputs

- HTML evidence pack

## Technologies

- Python
- PyTest
- HTML Report Generation

## Lineage
 
Each metric can be traced back to source datasets used in report generation.