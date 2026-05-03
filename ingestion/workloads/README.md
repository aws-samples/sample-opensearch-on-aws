# Dataset Processing Framework

A workload-based framework for processing data from various sources and loading it to S3 for OpenSearch Ingestion (OSI). Use this framework to load data into the osi-load domain. 

The framework engages with a workload that you define in the workloads folder. Workloads have these main components: a data source that knows how to discover and read your specific data format, a transformer that cleans and enriches your raw data into structured records, an index configuration that defines the OpenSearch field mappings and settings, and a configuration file that specifies processing parameters like batch sizes and document limits. Each workload is completely self-contained, allowing you to process different datasets with their own unique requirements while leveraging the same underlying framework. The data source component abstracts whether your data lives in local files or S3 buckets, automatically handling discovery and reading through a unified interface. The transformer component is where you implement your domain-specific logic for parsing, cleaning, and enriching data records before they're formatted for OpenSearch ingestion. The framework processes data in configurable batches, respects document limits for testing purposes, and automatically outputs properly formatted OpenSearch bulk API data to the S3 location expected by the OSI pipeline. Of course, you can do further manipulation of the records within OSI itself. The workload framework is intended to simplify the process of locating data and getting it into OSI.

## How to use the workloads framework

Before you do anything else, you must deploy the stack. The framework depends on the OpenSearch domain, S3 buckets, and IAM roles created by the CDK stack to function properly. Once deployed, the stack outputs provide the domain endpoint, master user credentials, and other configuration details that the framework uses to create indices and process data. Without a deployed stack, you won't be able to create OpenSearch indices or verify that your data processing pipeline works correctly.

### Create your workload directory and configuration

You define your workload by creating files and classes in the `<project root>/workloads` directory. You can find an example for a file-system based workload, and for an S3-based workload. Create a new sub folder in the workloads directory with your workload name, and if you like, copy the files from one of the examples there.

`cd <project root>`   
`mkdir workloads/<your workload name>`  
\[Optional: copy example files\] `cp workloads/example-s3-workload/* workloads/<your workload name>`

If you copied one of the examples, modify `config.py` (or create it based on one of the examples) with constants for your workload. For local file system workloads, set paths and file patterns. For S3 workloads, set a bucket and key prefix. In both cases, set the batch size, and maximum documents to deliver to OpenSearch.

### Create your destination index

You create your OpenSearch index using the `dataset/create_index.py` script and your workload name. The script expects to find an `index_settings.json` file in your workload directory. It uses the CloudFormation exports from the stack to find the domain endpoint, and admin user credentials. The destination index name is hard-coded as a constant to match the S3 sink in the OSI pipeline. If you want to change index name, be sure to change it in both places!

Once you've created or modified index_settings.json, you can 

```bash
cd <project-root>
python dataset/create_index.py <your workload name>
```

You can use the --delete-existing flag to overwrite the index if it already exists. WARNING! this flag will delete the index and any data in it, so use with caution!

## Create a custom workload

You should be able to accomplish a lot with the existing example workloads. However, if you want to bring in data from a different source (a database, for example), you can implement two main classes in your workload directory: a Data Source class and a Transformer class.

Creating a custom workload requires implementing the `WorkloadDataSource` class in `data_source.py` and the `WorkloadTransformer` class in `transformer.py`. For most common scenarios, your data source class inherits from either `FileSource` or `S3Source` depending on your data location, while also mixing in your transformer class to provide data processing capabilities. The `FileSource` class handles local file discovery and reading, while `S3Source` manages S3 object enumeration and retrieval.

However, if your data comes from a different source entirely—such as a database, API endpoint, or streaming service—you should inherit directly from `BaseDataSource` instead. This base class provides the core framework interface without any assumptions about file systems or S3 storage. When inheriting from `BaseDataSource`, you'll need to implement your own data discovery and retrieval logic within the `process_sources` method, handling connection management, query execution, and data fetching according to your specific source requirements.

### Implementing the Data Source Class

The `WorkloadDataSource` class must implement the `process_sources` method, which takes a list of source locations as input and orchestrates the complete data processing pipeline. This method reads data from each source, calls `transform_data` to clean and structure the raw data, then calls `process_records` to convert the structured data into individual document records, finally yielding batches of records ready for OpenSearch ingestion. This method typically overrides the default source discovery behavior by importing your workload's configuration and using it to find the actual data sources. In the example file workload, this involves calling `self.find_files()` with configured file patterns and base paths to discover local files. In the example S3 workload, an empty list is passed to the superclass since the S3Source base class automatically discovers objects based on the configured bucket and prefixes. For custom workloads inheriting from `BaseDataSource`, you would implement your own source discovery logic—such as executing database queries to identify tables or records, making API calls to enumerate available data endpoints, or connecting to streaming services to establish data feeds.

### Implementing the Transformer Class

Your transformer class inherits from `BaseTransformer` and defines how to parse and transform your specific data format. The example file transformer handles CSV data by using Python's csv module to parse the raw string content into dictionaries, while the example S3 transformer processes JSON Lines format by splitting the input on newlines and parsing each line as JSON. Both examples add processing metadata like timestamps and source type identifiers before yielding records.

To create a custom transformer, implement the two required abstract methods from `BaseTransformer`. The `transform_data` method receives raw data (typically a string containing file contents or S3 object data) and returns a cleaned, structured representation of that data. For CSV files, this might involve parsing the string into a list of dictionaries using Python's csv module. For JSON Lines files, this would involve splitting the string by newlines and parsing each line as JSON. For XML data, you might use an XML parser to convert the string into a tree structure or list of elements. The method should handle any data cleaning, validation, or initial transformation needed to convert the raw input into a workable format.

The `process_records` method takes the structured data from `transform_data` and yields individual records formatted for OpenSearch ingestion. This method receives the output of `transform_data` as input and must return an iterator that yields dictionaries representing individual documents. Each yielded dictionary becomes a document in your OpenSearch index. This is where you perform any final transformations, add metadata fields like processing timestamps, and ensure each record has the proper structure for your index mapping. This separation allows you to handle the parsing complexity in `transform_data` while keeping the record-level processing logic clean in `process_records`.

The framework provides a helper method called `add_document_id` that you should call for each record to ensure proper document ID handling. This method copies the value from your configured `document_id_field` to the standardized `osi_load_doc_id` field that the OpenSearch Ingestion pipeline expects. You simply call `record = self.add_document_id(record, CONFIG)` for each record before yielding it.

### Configuration Requirements

Your workload configuration in `config.py` must define a `CONFIG` dictionary containing the parameters your data source and transformer need. For file-based workloads, this includes `file_patterns` (a list of glob patterns to match files), `base_paths` (directories to search), `batch_size` (number of records per batch), `max_documents` (limit for testing), and `document_id_field` (the field name containing unique document identifiers). For S3-based workloads, replace `file_patterns` and `base_paths` with `s3_bucket` (source bucket name), `s3_prefixes` (list of key prefixes to process), and `region` (AWS region).

For custom data sources that inherit from `BaseDataSource`, you define whatever configuration parameters your specific source requires. Database workloads might include connection strings, table names, query parameters, and authentication credentials. API-based workloads could specify endpoints, authentication tokens, pagination settings, and request parameters. The framework passes your entire `CONFIG` dictionary to both your data source and transformer classes, allowing you to access any custom parameters needed for your specific implementation.

The framework handles all the complex orchestration of reading data sources, applying your transformations, batching records, and uploading to the destination S3 bucket in the format expected by OpenSearch Ingestion. Your custom implementation only needs to focus on the specifics of discovering your data sources and transforming your particular data format into the structure required by your OpenSearch index. 

## Workload Structure

Each workload contains:
```
workloads/workload_name/
├── index_settings.json    # OpenSearch index configuration
├── data_source.py        # WorkloadDataSource class
├── transformer.py        # WorkloadTransformer class
└── config.py            # CONFIG dictionary
```
