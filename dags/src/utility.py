##########################################################
###### UTILITY MODULE FOR AIRFLOW DAGs. ##################
## Description: This script contains functions utilized by the Airflow DAGs as python callables.
##
## Functions:
##     - fetch_payload: Function to retrieve data from an API source, sets the retrieved parameter as an input variable
##     - download_geodata: Function downloads the geo specific data related to the downstream DAG 
##     - clip: Function clips the downloaded geo-data using the inputs generated from the upstream DAG per requirements.
##     - load: Function to load the transformed data into a destination S3 bucket
#################################################
## Project: BMI Thünen Institute Breedfides
## Date: 26.10.2023
## Status: prod/dev
#################################################
## Comments:
##################################################

import json
import ast
import ftplib
import os
import gzip
import io
import re
import xarray as xr
import dask
import glob
from osgeo import ogr, osr
from owslib.wfs import WebFeatureService
from owslib.wcs import WebCoverageService
import logging
from datetime import datetime
import geopandas as gpd
from shapely.geometry import box, MultiPolygon
from dotenv import load_dotenv
from airflow.models import DagRun
import requests

from airflow.providers.amazon.aws.transfers.local_to_s3 import LocalFilesystemToS3Operator

### GLOBAL VARS
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
setup = os.getenv("SETUP", "PRODUCTION")
if setup == "DOCKER":
    current_dir = f"{os.getcwd()}/" ## Uncomment this and comment the next line if testing is on local-env (not prod server)
elif setup == "PRODUCTION":
    current_dir = os.path.expanduser(os.path.join("~", 'airflow-etl/'))

# Specify the path to the .env file
dotenv_path = os.path.join(current_dir, 'dags', 'src', '.env')

# Load environment variables from the .env file
load_dotenv(dotenv_path)


# Get today's date and time
date_now = datetime.now().strftime("%Y%m%d_%H%M")

### Initialize Spatial reference for User's Lat/Long coordinates (WGS84 coordinate system)
SRS_LATLON = osr.SpatialReference()
SRS_LATLON.ImportFromEPSG(4326)
SRS_UTM = osr.SpatialReference() ## Initialize spatial reference for a projected coordinate system UTM (Universal Transverse Mercator) Zone 32 North
SRS_UTM.ImportFromEPSG(32632)

def fetch_soilgrids(**kwargs):
    params = kwargs['params'].get('input_attributes', kwargs['params'])
    input_var, geo_tag = ast.literal_eval(params) if type(params) == str else params, kwargs['dag'].tags[-1] 
    
    logger.info(f"geo_tag: {geo_tag}")
    logger.info(f"input_var: {input_var}")
    run_id = kwargs['dag_run'].run_id
    logger.info(f"run id: {run_id}")

    url = 'https://rest.isric.org/soilgrids/v2.0/properties/query?'
    payload = {'lon': input_var['long'], 'lat': input_var['lat']}

    r = requests.get(url, params=payload)
    
    if r.status_code == 200:
        logger.info(f"fetch was succesfull for: {r.url}")
        file = os.path.join(current_dir, 'output','soilgrids', run_id, f'soilgrids_{date_now}.json')
        os.makedirs(os.path.dirname(file), exist_ok=True)
        with open(file, 'w', encoding='utf-8') as f:
            json.dump(r.json(), f, ensure_ascii=False, indent=4)
            logger.info(f"file written as : {file}")
        
        file = os.path.join(current_dir, 'output','soilgrids', run_id, f'soilgrids_{date_now}-metadata.txt')
        metadata = {
                'latitude': input_var['lat'],
                'longitude': input_var['long'],
                'DAG_ID':  kwargs['dag'].dag_id,
                'DAG_RUN_ID': run_id,
                'execution_start_time': kwargs['ts']
            }
        write_metadata(filepath=file, metadata=metadata)
        logger.info(f"file written as : {file}")
    else:
        logger.error(f"fetching was not successfull. status code {r.status_code}")

def fetch_payload(**kwargs):
    """
    Description: The `fetch_payload` function listens to POST requests made on airflow's API endpoint, retrieves the payload and 
                 sets it as input parameters to be used by other downstream DAGs 
    """
    params = kwargs['params']
    
    try:      
        kwargs['ti'].xcom_push(key='payload_key', value=params) ## Returns INPUT attributes for downstream tasks
        
    except Exception as e:
        logger.error(f"An error occured while extracting the JSON payload: {e}")
        raise
    

def get_latest_files(directory):
    """
    Description: The `get_latest_file` function returns the most recent filename on the output directory
     
    Output: A string value denoting the output's filename
    """
    try:
        files = [os.path.join(root, file) for root, dirs, files in os.walk(os.path.join(current_dir, directory)) for file in files]
        latest_files = sorted(files, key=lambda x: os.path.getmtime(x), reverse=True)[:2]
        return latest_files

    except Exception as e:
        logger.error(f"An error occurred while returning the filenames: {e}")
        raise
    

def write_metadata(filepath, metadata):
    """
    Description: The `write_metadata` function create a .txt file on the output directory and writes the metadata for each DAG execution
    """
    with open(filepath, 'w') as file:
        for key, value in metadata.items():
            file.write(f"{key}: {value}\n")
            

def write_to_s3(local_files, **kwargs):
    """
    Description: The `write_to_s3` function writes both the clipped output and its metadata to the object store container
    """
    for local_file in local_files:
        filetype = local_file.split(".")[-1]
        logger.info(f"local_file : {local_file}")
        logger.info(f"local_files : {local_files}")
        dag_id = kwargs['dag'].dag_id
        run_id = kwargs['dag_run'].run_id
        task = LocalFilesystemToS3Operator(
            task_id=f'write_{filetype}_output', 
            filename=local_file,
            dest_key= run_id.split('-')[0] + '/' + dag_id + '/' + local_file.rsplit('/')[-1],
            dest_bucket='BreedFidesETL-OBS',
            aws_conn_id='aws_breedfides_obs',
            replace=True        
        )
        
        task.execute(context=kwargs)

def write_wcs_to_s3(local_files, **kwargs):
    """
    Description: The `write_to_s3` function writes both the clipped output and its metadata to the object store container
    """
    for local_file in local_files:
        filetype = local_file.split(".")[-1]
        
        task = LocalFilesystemToS3Operator(
            task_id=f'write_{filetype}_output', 
            filename=local_file,
            dest_key='soilgrids',
            dest_bucket='BreedFidesETL-OBS',
            aws_conn_id='aws_breedfides_obs',
            replace=True
        )
        
        task.execute(context=kwargs)


def get_most_recent_dag_run(dag_id):
    """
    Description:
        The `get_most_recent_dag_run` function returns the execution time of the previous DAG, this function is called by the ExternalDAGSensor operator

    Args:
        dag_id (_type_): Name of External DAG

    Returns:
        _type_: DateTime / None
    """
    dag_runs = DagRun.find(dag_id=dag_id)
    dag_runs.sort(key=lambda x: x.execution_date, reverse=True)

    if dag_runs:
        return dag_runs[0].execution_date
    else:
        return None

    
def download_geodata(**kwargs):
    """
    Description: The `download_geodata` function fetches the related geo/climate related data from independent sources (API or FTP), the retrieved data will
                 be clipped subsequently using the input attrbutes
    """
    params = kwargs['params'].get('input_attributes', kwargs['params'])
    input_var = ast.literal_eval(params) if type(params) == str else params 
    logger.info(f"input is: {input_var}")
    dag_id = kwargs['dag'].dag_id ## Extracts DAG-ID from context object
    ftp_dir = kwargs['dag'].tags[-1] ## Extract Tags
    local_file_path = current_dir + ftp_dir
    
    try:
        if dag_id == 'fetch_wcs':
            wcs_url, cov_id = input_var['wcs_url'], input_var['cov_id']
            
            logger.info(f"Fetching Geo data from {wcs_url}")
            metadata = {
            'bbox': input_var['bbox'],
            'wcs_url': wcs_url,
            'cov_id': cov_id,
            }

            wcs = WebCoverageService(wcs_url, version='2.0.1')

            response = wcs.getCoverage(
                identifier = [cov_id], 
                crs = input_var['srsname'],
                subsets = input_var['bbox'], 
                format = 'image/tiff')
        
            ## get rid of non ascii characters for filename:
            filename = ''.join(e for e in cov_id if e.isalnum())

            ## write response to hdd as json file:
            logger.info(f"Writing coverage file to breedfides-airflow/wcs/{filename}_{date_now}.tif")
            output_dir_path = os.path.join(current_dir, 'wcs', f'{filename}_{date_now}.tif')
            with open(output_dir_path, 'wb') as file:
                file.write(response.read())
            write_metadata(filepath=output_dir_path + "_metadata.txt", metadata=metadata)
            
        elif dag_id == 'fetch_wfs':
            typename = input_var['typename']
            filename = typename.replace(':', '_')
            
            wfs_url = input_var['wfs_url']
            wfs = WebFeatureService(url=wfs_url, version='1.1.0')
        
            logger.info(f"Fetching Geospatial feature data from {wfs_url}")
            response = wfs.getfeature(
                typename = typename,
                bbox = input_var['bbox'],
                srsname = input_var['srsname'],
                outputFormat = 'json').read().decode('utf-8')
            
            response = json.loads(response) ### CONVERT TO JSON OBJECT
        
            logger.info(f"Writing feature data to breedfides-airflow/wfs/{filename}.json")
            output_dir_path = os.path.join(current_dir, 'wfs', f'{filename}.json')
            with open(output_dir_path, 'w') as json_file:
                json.dump(response, json_file)
                
        elif 'fetch_cdc' in dag_id:
            ftp = ftplib.FTP('opendata.dwd.de', timeout=3600)
            
            ftp.login() ### Login to FTP site
            
            remote_directory = f"/climate_environment/CDC/grids_germany/daily/Project_TRY/{ftp_dir}"
            ftp.cwd(remote_directory)
            
            ftp_files = set(obj[:-3] for obj in ftp.nlst() if '_20' in obj) ### List the files in the remote FTP directory (data from year 2000)
            net_cdf_files = set(os.path.basename(fp) for fp in glob.glob(f'{local_file_path}/*.nc')) ### List the files in the local directory
            
            file_exists, files_to_download = verify_file_exists(net_cdf_files, ftp_files)
            
            if file_exists:
                pass
            
            else: 
                ## Download each file
                for file in files_to_download:
                    logger.info(f"Downloading file : {file}")
                    remote_file = io.BytesIO() ## Open remote file 
                    ftp.retrbinary('RETR ' + file, remote_file.write)
                    remote_file.seek(0)
                    
                    ## Decompress GZIP file
                    with gzip.GzipFile(fileobj=remote_file, mode='rb') as gzipped_file:
                        decompressed_content = gzipped_file.read() ## Read the decompressed content
                
                    logger.info(f"Writing netCDF data to {local_file_path}")
                
                    with open(f"{os.path.join(f'{local_file_path}/', os.path.splitext(file)[0])}", 'wb') as local_file:
                        local_file.write(decompressed_content)
                        
            ## Close the FTP connection
            ftp.quit()
            
    except Exception as e:
        logger.error(f"An error occured while extracting the GeoNetwork data: {e}")
        raise 
    
   
def clip_data(**kwargs):
    """
    Description: The `clip_data` function utilizes the input attributes (the lattitude and longitude), creates a buffer-extent using both coordinates and a buffer,
                 it then converts the buffer-extent to min/max lat-long coordinates which in turn uses these attributes to clip the downloaded netCDF4 geodata
    """
    params = kwargs['params'].get('input_attributes', kwargs['params'])
    input_var, geo_tag = ast.literal_eval(params) if type(params) == str else params, kwargs['dag'].tags[-1] 
    directory = current_dir + geo_tag
    
    try:
        latitude, longitude, buffer_in_metres = input_var['lat'], input_var['long'], 3000 if 'buffer_in_metres' not in input_var else int(input_var['buffer_in_metres'])
        dag_id, dag_run_id, start_time = kwargs['dag'].dag_id, kwargs['dag_run'].run_id, kwargs['ts']
        metadata = {
            'latitude': latitude,
            'longitude': longitude,
            'buffer_in_metres': buffer_in_metres,
            'DAG_ID': dag_id,
            'DAG_RUN_ID': dag_run_id,
            'execution_start_time': start_time
        }     
        
        buffer_extent = compute_buffer_extent(longitude, latitude, buffer_in_metres)
        lat_min, lat_max, long_min, long_max = convert_buffer_extent(buffer_extent)
        
        dataset = xr.open_mfdataset(f'{directory}/*.nc', decode_times=True, 
                                    chunks={'time': 1000000, 'latitude':1, 'longitude':1}, engine='netcdf4', 
                                    data_vars='minimal', coords='minimal', compat='override', parallel=True, autoclose=True)
        
        # Clip the Array based on the converted buffer extent and set a boolean mask
        mask_lon = (dataset.lon >= long_min) & (dataset.lon <= long_max)
        mask_lat = (dataset.lat >= lat_min) & (dataset.lat <= lat_max)

        mask_lon = mask_lon.compute()
        mask_lat = mask_lat.compute()
        
        # Apply the masks to subset the dataset
        subset_ds = dataset.where(mask_lon & mask_lat, drop=True)
          
        # Write clipped output as netCDF
        output_path = os.path.join(current_dir, 'output', geo_tag, dag_run_id, f'{geo_tag}_{date_now}.nc')
        ## Create output-path
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        logger.info(f"Writing clipped data to {output_path}")
        subset_ds.to_netcdf(output_path, format='netcdf4', compute=True)
        dataset.close()
        
        # Write the metadata for the associated clipped output
        write_metadata(os.path.join(current_dir, 'output', geo_tag, dag_run_id, f'{geo_tag}_{date_now}_metadata.txt'), metadata)
        
    except Exception as e:
        logger.error(f"An error occured while clipping the GeoNetwork data: {e}")
        raise 



def clip_soil_data(**kwargs):
    """
    Description: The `clip_soil_data` function utilizes the input attributes (the lattitude and longitude), creates a buffer-extent using both coordinates and a buffer,
                 it then converts the buffer-extent to min/max lat-long coordinates which in turn uses these attributes to clip the downloaded soil geopackage data
    """
    params = kwargs['params'].get('input_attributes', kwargs['params'])
    input_var, geo_tag = ast.literal_eval(params) if type(params) == str else params, kwargs['dag'].tags[-1] 
    directory = os.path.join(current_dir, geo_tag, 'BUEK_data.gpkg')
    
    try:
        latitude, longitude, buffer_in_metres = input_var['lat'], input_var['long'], 3000 if 'buffer_in_metres' not in input_var else int(input_var['buffer_in_metres'])
        dag_id, dag_run_id, start_time = kwargs['dag'].dag_id, kwargs['dag_run'].run_id, kwargs['ts']
        metadata = {
            'latitude': latitude,
            'longitude': longitude,
            'buffer_in_metres': buffer_in_metres,
            'DAG_ID': dag_id,
            'DAG_RUN_ID': dag_run_id,
            'execution_start_time': start_time
        }  

        gdf = gpd.read_file(directory, engine='pyogrio', use_arrow=True)
        buffer_extent = compute_buffer_extent(longitude, latitude, buffer_in_metres)
        miny, maxy, minx, maxx = convert_buffer_extent(buffer_extent)

        ## Transform crs from EPSG:25832 (default CRS) to EPSG:4326 (Lat/Long coordinate system)
        gdf = gdf.to_crs('EPSG:4326')

        ## Assign buffer-extents to bounding-box and initialize bbox as MultiPolgons
        bbox = box(minx, miny, maxx, maxy)
        multi_bbox = MultiPolygon([bbox])

        # Create a GeoDataFrame for the bounding box, to be used later for clipping
        bbox_gdf = gpd.GeoDataFrame(geometry=[multi_bbox], crs=gdf.crs)

        ## Clip the soil geo file based on the bounding box buffer extent and set the crs back to its default value EPSG:25832")
        clipped_df = gpd.clip(gdf, bbox_gdf)
        clipped_df = clipped_df.to_crs('EPSG:25832')

        # Write clipped output as .gpkg
        output_path = os.path.join(current_dir, 'output', geo_tag, dag_run_id, f'{geo_tag}_{date_now}.gpkg')
        ## Create output-path
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        logger.info(f"Writing clipped data to {output_path}")
        clipped_df.to_file(output_path, driver='GPKG')
        
        # Write the metadata for the associated clipped output
        write_metadata(os.path.join(current_dir, 'output', geo_tag, dag_run_id, f'{geo_tag}_{date_now}_metadata.txt'), metadata)
        
    except Exception as e:
        logger.error(f"An error occured while clipping the GeoPackage data: {e}")
        raise 
    
    

def verify_file_exists(local, ftp):
    """
    Description: The `verify_file_exists` function compares two sets of filenames, one representing files locally and the other obtained from an FTP server. 
    
    Output: It returns a tuple indicating whether the sets are identical (True OR False) and a set of differing items 
            which will then be downloaded to the local filesystem 
    """
    differing_items = set(item + '.gz' for item in local.symmetric_difference(ftp))
    
    return local == ftp, differing_items

    
def compute_buffer_extent(long, lat, buffer_in_metres):
    """
    Description: The `compute_buffer_extent` function calculates the bounding box (extent) of a buffer 
                 around a specified point defined by user's input (i.e. latitude and longitude). The buffer is created with a user-defined 
                 radius in meters (3000 metres). 
    
    Output: The resulting extent is provided as a tuple representing the minimum and maximum coordinates in the UTM Zone 32 North coordinate reference system.
    """    
    try:
        ### creation of point geometry with user / front end lat-long values
        point = ogr.Geometry(ogr.wkbPoint)
        point.AddPoint(float(long), float(lat))

        ## Transform point from the WGS84 (latitude and longitude) coordinate system (SRS_LATLON) to the UTM Zone 32 North coordinate system (SRS_UTM)
        proj_point = osr.CoordinateTransformation(SRS_LATLON, SRS_UTM)
        point.Transform(proj_point)

        # buffer creation and buffer extent as output
        buffer_geom = point.Buffer(buffer_in_metres)

        buffer_extent = buffer_geom.GetEnvelope()
        
        return buffer_extent
        
    except Exception as e:
        logger.error(f"An error occured while computing the buffer extent: {e}")
    
        
def convert_buffer_extent(buffer_extent):
    """
    Description: The `convert_buffer_extent` function transforms coordinates from a given buffer extent in the UTM Zone 32 North coordinate reference system 
                 to latitude and longitude positions in the WGS84 standard. 
    
    Output: The resulting coordinates represent the minimum and maximum latitude and longitude values.
    """
    try:
        # Convert UTM coordinates back to latitude and longitude
        proj_extent_to_latlon = osr.CoordinateTransformation(SRS_UTM, SRS_LATLON)
        
        # Perform the transformation
        long_min, lat_min, _ = proj_extent_to_latlon.TransformPoint(buffer_extent[0], buffer_extent[2])
        long_max, lat_max, _ = proj_extent_to_latlon.TransformPoint(buffer_extent[1], buffer_extent[3])
        
        return lat_min, lat_max, long_min, long_max
    
    except Exception as e:
        logger.error(f"An error occured while converting the buffer extent to lat/long positions: {e}")
