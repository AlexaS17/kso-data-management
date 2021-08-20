import io, os, json, csv
import sqlite3
import requests, argparse
import pandas as pd
import numpy as np

from datetime import datetime
from panoptes_client import Project, Panoptes
import utils.db_utils as db_utils
from utils.zooniverse_utils import auth_session

# Function to extract the metadata from subjects
def extract_metadata(subj_df):

    # Reset index of df
    subj_df = subj_df.reset_index(drop=True).reset_index()

    # Flatten the metadata information
    meta_df = pd.json_normalize(subj_df.metadata.apply(json.loads))

    # Drop metadata and index columns from original df
    subj_df = subj_df.drop(columns=["metadata", "index",])

    return subj_df, meta_df


# Function to process subjects uploaded automatically
def auto_subjects(subjects_df, auto_date):
    
    # Select automatically uploaded frames
    auto_subjects_df = subjects_df[(subjects_df["created_at"] > auto_date)]

    # Extract metadata from automatically uploaded subjects
    auto_subjects_df, auto_subjects_meta = extract_metadata(auto_subjects_df)
    
    # Combine metadata info with the subjects df
    auto_subjects_df = pd.concat([auto_subjects_df, auto_subjects_meta], axis=1)
    
    return auto_subjects_df
    
    
# Function to process subjects uploaded manually
def manual_subjects(subjects_df, manual_date, auto_date):
    
    # Select clips uploaded manually
    man_clips_df = (
        subjects_df[
            (subjects_df["metadata"].str.contains(".mp4"))
            & (
                subjects_df["created_at"].between(
                    manual_date, auto_date
                )
            )
        ]
        .reset_index(drop=True)
        .reset_index()
    )

    # Specify the type of subject
    man_clips_df["subject_type"] = "clip"

    # Extract metadata from manually uploaded clips
    man_clips_df, man_clips_meta = extract_metadata(man_clips_df)

    # Process the metadata of manually uploaded clips
    man_clips_meta = process_manual_clips(man_clips_meta)

    # Combine metadata info with the subjects df
    man_clips_df = pd.concat([man_clips_df, man_clips_meta], axis=1)
    
    return man_clips_df
    
    # Function to get the movie_ids based on movie filenames
def get_movies_id(df, db_path):

    # Create connection to db
    conn = db_utils.create_connection(db_path)

    # Query id and filenames from the movies table
    movies_df = pd.read_sql_query("SELECT id, filename FROM movies", conn)
    movies_df = movies_df.rename(
        columns={"id": "movie_id", "filename": "movie_filename"}
    )

    # Check all the movies have a unique ID
    df_unique = df.movie_filename.unique()
    movies_df_unique = movies_df.movie_filename.unique()
    
    diff_filenames = set(df_unique).difference(movies_df_unique)
    
    if diff_filenames:
        print(
            f"There are clip subjects that don't have movie_id. The movie filenames are {diff_filenames}"
        )
        
        raise
    
    # Reference the manually uploaded subjects with the movies table
    df = pd.merge(df, movies_df, how="left", on="movie_filename")

    # Drop the movie_filename column
    df = df.drop(columns=["movie_filename"])

    return df


# Function to process the metadata of clips that were uploaded manually
def process_manual_clips(meta_df):

    # Select the filename of the clips and remove extension type
    clip_filenames = meta_df["filename"].str.replace(".mp4", "")

    # Get the starting time of clips in relation to the original movie
    # split the filename and select the last section
    meta_df["clip_start_time"] = (
        clip_filenames.str.rsplit("_", 1).str[-1]
    )

    # Extract the filename of the original movie
    meta_df["movie_filename"] = meta_df.apply(
        lambda x: x["filename"].replace("_" + x["clip_start_time"], "").replace(".mp4", ".mov"), axis=1
    )
  
    # Get the end time of clips in relation to the original movie
    meta_df["clip_start_time"] = pd.to_numeric(
        meta_df["clip_start_time"], downcast="signed"
    )
    meta_df["clip_end_time"] = meta_df["clip_start_time"] + 10

    # Select only relevant columns
    meta_df = meta_df[
        ["filename", "movie_filename", "clip_start_time", "clip_end_time"]
    ]
        
    return meta_df


# Function to select the first subject of those that are duplicated
def clean_duplicates(subjects, duplicates_csv):
    
    # Load the csv with information about duplicated subjects
    dups_df = pd.read_csv(duplicates_csv)
    
    # Include a column with unique ids for duplicated subjects 
    subjects = pd.merge(subjects, dups_df, how="left", left_on="subject_id", right_on="dupl_subject_id")
    
    # Replace the id of duplicated subjects for the id of the first subject
    subjects.subject_id = np.where(subjects.single_subject_id.isnull(), subjects.subject_id, subjects.single_subject_id)
    
    #Select only unique subjects
    subjects = subjects.drop_duplicates(subset='subject_id', keep='first')
    
    return subjects

def remove_duplicates(user: str, password: str, db_path: str = r"koster_lab.db", 
                      duplicates_csv: str = r"../db_starter/db_csv_info/duplicated_subjects.csv"):
    
    # Connect to the Zooniverse project
    project = auth_session(user, password)

    # Get info of subjects uploaded to the project
    export = project.get_export("subjects")

    # Save the subjects info as pandas data frame
    subjects_df = pd.read_csv(
        io.StringIO(export.content.decode("utf-8")),
        usecols=[
            "subject_id",
            "metadata",
            "created_at",
            "workflow_id",
            "subject_set_id",
            "classifications_count",
            "retired_at",
            "retirement_reason",
        ],
    )

    
    #TODO option for dealing with Koster specific upload system (e.g if project=#9747 ...)
    
    ### Update subjects automatically and manually uploaded separately ###

    # Specify the date when the metadata of subjects uploaded matches schema.py
    auto_date = "2020-05-29 00:00:00 UTC"
    
    # Specify the starting date when clips were manually uploaded
    manual_date = "2019-11-17 00:00:00 UTC"
    
    # Select automatically uploaded subjects
    auto_subjects_df = auto_subjects(subjects_df, auto_date = auto_date)

    #################Koster specific########
    # Select manually uploaded subjects
    manual_subjects_df = manual_subjects(subjects_df, manual_date = manual_date, auto_date = auto_date)

    # Include movie_ids to the metadata
    manual_subjects_df = get_movies_id(manual_subjects_df, db_path)
    
    # Combine all uploaded subjects
    subjects = pd.merge(manual_subjects_df, auto_subjects_df, how="outer")
    
    # Clear duplicated subjects if any
    if args.duplicates_csv:
        subjects = clean_duplicates(subjects, duplicates_csv)
        
        
    ################End of koster specific###############
    
    ### Update subjects table ###
    
    # Set subject_id information as id
    subjects = subjects.rename(columns={"subject_id": "id"})

    # Set the columns in the right order
    subjects = subjects[
        [
            "id",
            "subject_type",
            "filename",
            "clip_start_time",
            "clip_end_time",
            "frame_exp_sp_id",
            "frame_number",
            "workflow_id",
            "subject_set_id",
            "classifications_count",
            "retired_at",
            "retirement_reason",
            "created_at",
            "movie_id",
        ]
    ]

    # Ensure that subject_ids are not duplicated by workflow
    subjects = subjects.drop_duplicates(subset='id')
    
    # Test table validity
    db_utils.test_table(subjects, "subjects", keys=["movie_id"])

    # Add values to subjects
    db_utils.add_to_table(
        db_path, "subjects", [tuple(i) for i in subjects.values], 14
    )
