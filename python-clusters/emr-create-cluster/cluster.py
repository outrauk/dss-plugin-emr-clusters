import boto3
import dku_emr
import os, json, argparse, logging
from dataiku.cluster import Cluster

# This actually belongs in the main entry point
logging.basicConfig(format='%(asctime)s [%(levelname)s] %(message)s', level=logging.INFO)
logging.getLogger().setLevel(logging.INFO)

class MyCluster(Cluster):
    def __init__(self, cluster_id, cluster_name, config, plugin_config):
        self.cluster_id = cluster_id
        self.cluster_name = cluster_name
        self.config = config
        self.plugin_config = plugin_config
        
    def start(self):
        region = self.config.get("awsRegionId") or dku_emr.get_current_region()
        client = boto3.client('emr', region_name=region)
        release = 'emr-%s' % self.config["emrVersion"]

        name = "DSS cluster id=%s name=%s" % (self.cluster_id, self.cluster_name)

        logging.info("starting cluster, release=%s name=%s" % (release, name))

        extraArgs = {}
        if "logsPath" in self.config:
            extraArgs['LogUri'] = self.config["logsPath"]
        if "securityConfiguration" in self.config:
            extraArgs["SecurityConfiguration"] = self.config["securityConfiguration"]
        if self.config.get("ebsRootVolumeSize", 0):
            extraArgs["EbsRootVolumeSize"] = self.config["ebsRootVolumeSize"]
        
        security_groups = []
        if "additionalSecurityGroups" in self.config:
            security_groups = [x.strip() for x in self.config["additionalSecurityGroups"].split(",")]

        subnet = self.config.get("subnetId") or dku_emr.get_current_subnet()

        instances = {
                'InstanceGroups': [{
                    'InstanceRole': 'MASTER',
                    'InstanceType': self.config["masterInstanceType"],
                    'InstanceCount': 1
                }],
                'KeepJobFlowAliveWhenNoSteps': True,
                'Ec2SubnetId': subnet,
                'AdditionalMasterSecurityGroups': security_groups,
                'AdditionalSlaveSecurityGroups': security_groups
            }
        
        if self.config.get("coreInstanceCount"):
            if not self.config.get("coreInstanceType"):
                raise Exception("Missing core instance type")
            instances['InstanceGroups'].append({
                'InstanceRole': 'CORE',
                'InstanceType': self.config["coreInstanceType"],
                'InstanceCount': self.config["coreInstanceCount"]
            })

        if self.config.get("taskInstanceCount"):
            if not self.config.get("taskInstanceType"):
                raise Exception("Missing task instance type")
            instances['InstanceGroups'].append({
                'InstanceRole': 'TASK',
                'InstanceType': self.config["taskInstanceType"],
                'InstanceCount': self.config["taskInstanceCount"]
            })

        if "ec2KeyName" in self.config:
            instances['Ec2KeyName'] = self.config["ec2KeyName"]

        tags = [{'Key': 'Name', 'Value': name}]
        for tag in self.config.get("tags", []):
            tags.append({"Key" : tag["from"], "Value" : tag["to"]})
        
        if self.config["metastoreDBMode"] == "CUSTOM_JDBC":
            props = {
                "javax.jdo.option.ConnectionURL" : self.config["metastoreJDBCURL"],
                "javax.jdo.option.ConnectionDriverName": self.config["metastoreJDBCDriver"],
                "javax.jdo.option.ConnectionUserName": self.config["metastoreJDBCUser"],
                "javax.jdo.option.ConnectionPassword": self.config["metastoreJDBCPassword"],
            }
            Configurations = [{"Classification": "hive-site", "Properties" : props}]
            extraArgs["Configurations"] = Configurations
        elif self.config["metastoreDBMode"] == "MYSQL":
            props = {
                "javax.jdo.option.ConnectionURL" : "jdbc:mysql://%s:3306/hive?createDatabaseIfNotExist=true" % self.config["metastoreMySQLHost"],
                "javax.jdo.option.ConnectionDriverName": "org.mariadb.jdbc.Driver",
                "javax.jdo.option.ConnectionUserName": self.config["metastoreMySQLUser"],
                "javax.jdo.option.ConnectionPassword": self.config["metastoreMySQLPassword"]
            }
            Configurations = [{"Classification": "hive-site", "Properties" : props}]
            extraArgs["Configurations"] = Configurations
        elif self.config["metastoreDBMode"] == "AWS_GLUE_DATA_CATALOG":
            props = {
                "hive.metastore.client.factory.class": "com.amazonaws.glue.catalog.metastore.AWSGlueDataCatalogHiveClientFactory"
            }
            Configurations = [{"Classification": "hive-site", "Properties" : props}]
            extraArgs["Configurations"] = Configurations
        
        logging.info("Starting cluster: %s", dict(
            Name=name,
            ReleaseLabel=release,
            Instances=instances,
            Applications=[
                {"Name": "Hadoop"},
                {"Name": "Hive"},
                {"Name": "Tez"},
                {"Name": "Pig"},
                {"Name": "Spark"},
                {"Name": "Zookeeper"}
            ],
            VisibleToAllUsers=True,
            JobFlowRole=self.config["nodesRole"],
            ServiceRole=self.config["serviceRole"],
            Tags=tags,
            **extraArgs
        ))

        response = client.run_job_flow(
            Name=name,
            ReleaseLabel=release,
            Instances=instances,
            Applications=[
                {"Name": "Hadoop"},
                {"Name": "Hive"},
                {"Name": "Tez"},
                {"Name": "Pig"},
                {"Name": "Spark"},
                {"Name": "Zookeeper"}
            ],
            VisibleToAllUsers=True,
            JobFlowRole=self.config["nodesRole"],
            ServiceRole=self.config["serviceRole"],
            Tags=tags,
            **extraArgs
         )

        clusterId = response['JobFlowId']
        logging.info("clusterId=%s" % clusterId)
        
        logging.info("waiting for cluster to start")
        client.get_waiter('cluster_running').wait(ClusterId=clusterId)
        
        return dku_emr.make_cluster_keys_and_data(client, clusterId, create_user_dir=True, create_databases=self.config.get("databasesToCreate"))

    def stop(self, data):
        """
        Stop the cluster
        
        :param data: the dict of data that the start() method produced for the cluster
        """
        emrClusterId = data["emrClusterId"]

        region = self.config.get("awsRegionId") or dku_emr.get_current_region()
        client = boto3.client('emr', region_name=region)
        client.terminate_job_flows(JobFlowIds=[emrClusterId])
