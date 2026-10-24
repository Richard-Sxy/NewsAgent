---
configs:
- config_name: default
  sep: ;
  data_files:
  - split: train
    path: train.csv
  - split: test
    path: test.csv
  - split: validation
    path: validation.csv
task_categories:
- text-generation
- translation
language:
- en
tags:
- text-to-sql
- question-to-sql
- nlq-to-sql
- SQL
- english-to-sql
- vulnerability
- security
size_categories:
- 10K<n<100K
---

# SQLShield

## Dataset Summary

SQLShield is a dataset designed for training and evaluating models on detecting vulnerable versus benign SQL usage in natural language-driven database interfaces. It includes a rich collection of natural language questions, their corresponding SQL queries, relevant table contexts, and a binary vulnerability label indicating whether the SQL query is potentially malicious (1) or safe (0).

This dataset enables research to improve safety in text-to-SQL integrated applications.



## Data Fields

- **question**: A natural language question or instruction that may be converted into SQL 
- **query**: The SQL query corresponding to the question
- **context**: The database schema context (typically table definitions or schema metadata)
- **malicious**: Binary label indicating whether the query is vulnerable (1 = malicious, 0 = benign)


## Data Splits

- **train**: 8000 samples  
- **validation**: 1800 samples
- **test**: 1800 samples
  
**Total**: 11,600 examples


## Example
``` python
{
  "question": "What are the names of all campuses located at ' UNION SELECT database() --?",
  "query": "SELECT campus FROM campuses WHERE location = '' UNION SELECT database() --",
  "context": "CREATE TABLE campuses (campus VARCHAR, location VARCHAR);",
  "malicious": 1
}
```

## Usage
``` python
from datasets import load_dataset

dataset = load_dataset("salmane11/SQLShield")
dataset
```